# social-surveyor — Plan

A living plan document for `apurvam/social-surveyor`. Updated as sessions complete and decisions evolve.

---

## Product statement

social-surveyor is a self-hosted social listening pipeline for founders and small teams who want to monitor online conversations about their product space without paying $100–300/month for enterprise social listening tools (Octolens, Intently, Brandwatch, etc.). It polls a configurable set of sources (Reddit via RSS, Hacker News, X, GitHub, blog RSS feeds), dedupes and classifies matching posts with an LLM tuned to the user's specific ICP and competitive positioning, and routes results to Slack — high-urgency posts as immediate alerts, everything else as a daily digest.

The thesis is that a small Python process plus a well-tuned classifier prompt outperforms general-purpose social listening tools for a specific vertical, because the user owns the prompt and can iterate on it against real data in their domain. The cost is a few dollars a month of AWS infra, a few dollars of LLM API calls, and optional pay-per-use charges on the X API.

The project is designed to be forked. Each "project" (a monitoring configuration for a specific topic) lives under `projects/<n>/` with its own sources, classifier, routing, and eval set. The same engine runs multiple projects as separate systemd-managed processes. Initial use cases: monitoring for opendata-timeseries (observability cost complaints, durable Prometheus questions) and agent infrastructure market research.

---

## Architecture overview

### Pipeline shape

```
┌─────────────┐    ┌──────────┐    ┌───────────┐    ┌────────────┐    ┌──────────┐
│   sources   │ -> │  dedupe  │ -> │ prefilter │ -> │ classifier │ -> │  router  │
│ (cron poll) │    │ (sqlite) │    │  (regex)  │    │  (Haiku)   │    │ (urgency)│
└─────────────┘    └──────────┘    └───────────┘    └────────────┘    └──────────┘
                                                                            |
                                                          ┌─────────────────┴──────────┐
                                                          v                            v
                                                    ┌──────────┐              ┌───────────────┐
                                                    │ immediate│              │    digest     │
                                                    │  (Slack) │              │ (Slack, 9am)  │
                                                    └──────────┘              └───────────────┘
```

All stages run inside a single always-on Python process scheduled by APScheduler. Pollers run on 10-minute intervals, the digest runs once daily. Classification happens immediately after fetch so alerts are timely.

### Module boundaries

- `sources/` — one module per platform, all implementing the same `Source` ABC with a `fetch(since_id) -> list[RawItem]` method. Swapping in a new platform is ~100 lines. Supported in MVP: Reddit (via RSS), Hacker News, GitHub, X Recent Search. Added later: blog RSS feeds (Medium, Substack, Dev.to, Lobste.rs).
- `storage.py` — SQLite access layer. Tables: `items`, `source_cursors`, `api_usage`, plus `classifications` (session 3) and `alerts` (session 4). One database file per project.
- `classifier.py` — assembles the prompt from `classifier.yaml`, calls the Anthropic API, parses the JSON response. Stores `prompt_version` on each classification for regression tracking.
- `router.py` — reads classifications, decides immediate-vs-digest based on urgency threshold, deduplicates against the `alerts` table to avoid re-alerting on the same item.
- `notifier.py` — Slack webhook client. Block Kit formatting for rich alerts.
- `digest.py` — queries unsent classifications in the last 24h, formats a ranked summary, posts to the digest channel.
- `pipeline.py` — orchestrates a single pipeline run: fetch → dedupe → prefilter → classify → route → notify.
- `cli.py` — Typer-based CLI. Subcommands: `poll`, `classify`, `digest`, `backfill`, `explain`, `eval`, `handled`, `usage`.
- `config.py` — pydantic v2 models for every YAML file. Validates on load.
- `secrets.py` — resolves secret *names* referenced in YAML to actual values. Tries env vars first (local dev), falls back to AWS SSM Parameter Store (production). Configs never contain secret values.

### Project layout

```
social-surveyor/
├── PLAN.md                          # this file
├── CLAUDE.md                        # Claude Code project conventions
├── README.md                        # user-facing docs
├── LICENSE                          # MIT
├── pyproject.toml                   # uv-managed
├── uv.lock
├── .github/
│   └── workflows/
│       └── deploy.yml               # push-to-main → SSH deploy to EC2
├── src/social_surveyor/             # the engine, project-agnostic
│   ├── sources/
│   │   ├── base.py
│   │   ├── reddit.py                # RSS-based (primary, session 2.5)
│   │   ├── reddit_api.py            # PRAW-based (dormant, awaiting approval)
│   │   ├── hackernews.py
│   │   ├── github.py
│   │   └── x.py
│   ├── storage.py
│   ├── classifier.py
│   ├── router.py
│   ├── notifier.py
│   ├── digest.py
│   ├── pipeline.py
│   ├── cli.py
│   ├── config.py
│   ├── log_config.py
│   └── secrets.py
├── projects/
│   ├── example/                     # generic reference config for forkers
│   │   ├── sources/
│   │   │   ├── reddit.yaml
│   │   │   ├── hackernews.yaml
│   │   │   ├── github.yaml
│   │   │   └── x.yaml
│   │   ├── classifier.yaml
│   │   ├── routing.yaml
│   │   └── evals/labeled.jsonl
│   ├── opendata/                    # apurva's real project — public MVP, may split to private at 5.5
│   │   └── ... (same shape)
│   └── agent-infra/                 # apurva's second project
│       └── ... (same shape)
├── deploy/
│   ├── social-surveyor@.service     # systemd template unit
│   ├── deploy.sh                    # invoked by GitHub Actions
│   └── bootstrap-ec2.sh             # one-time EC2 provisioning script
├── data/                            # gitignored, populated at runtime
│   ├── opendata.db
│   └── agent-infra.db
└── tests/
```

### Multi-project execution model

Pattern A from planning discussion: one codebase, one engine, one process per project.

- CLI commands all take `--project <n>` (required).
- systemd template unit `social-surveyor@opendata.service` → runs `social-surveyor run --project opendata`.
- Adding a new project: create `projects/<n>/`, populate the four YAML files, `systemctl enable --now social-surveyor@<n>`. No code changes.
- Each process owns its own SQLite file, its own Slack channels, its own schedule.
- Shared: the engine, the Anthropic API key, the AWS credentials.

### Secret-reference pattern (day one, never revisited)

Configs contain secret *names*, not values. Example:

```yaml
# projects/opendata/routing.yaml
channels:
  immediate:
    webhook_secret: OPENDATA_SLACK_WEBHOOK_IMMEDIATE
  digest:
    webhook_secret: OPENDATA_SLACK_WEBHOOK_DIGEST
```

At startup, `secrets.py` resolves `OPENDATA_SLACK_WEBHOOK_IMMEDIATE`:

1. Check `os.environ` first (local dev, `.env` file loaded via python-dotenv)
2. Fall back to AWS SSM Parameter Store at `/social-surveyor/<secret_name>` (production)
3. Raise if neither has it

This means configs look identical in dev and prod, and swapping secret backends is a one-function change.

---

## Session plan

Each session is a focused Claude Code working session producing a reviewable diff. Sessions 1–5 are the MVP. Session 5.5 is conditional. Sessions 6–7 are enhancements.

### Session 1 — Skeleton + Reddit (PRAW) + real data collection

**Status:** Complete, merged. PRAW-based Reddit source written but never verified live (Reddit revoked self-service API access before creds could be obtained). Superseded by Session 2.5's RSS-based implementation. PRAW code preserved as `sources/reddit_api.py` for the day Responsible Builder approval comes through.

**Scope as built:** Project structure, config models, SQLite storage, PRAW-based Reddit source, multi-project support, dry-run mode, Reddit backfill. `projects/opendata/` seeded with real subreddit configs.

### Session 2 — Remaining core sources

**Status:** Complete, merged. Live-call verification deferred to Session 2.5.

**Scope as built:** Hacker News (Algolia), GitHub (issues + comments), X (Recent Search, pay-per-use with cost controls). All implement `Source` ABC. Session 1 cleanup items (log_config rename, SourceInitError, backfill diagnostic log) completed in Phase 0.

### Session 2.5 — Reddit RSS refactor + live verification

**Status:** Complete, merged. Phase B5/B6 (multi-source + since_id-twice) skipped by mutual agreement after B1-B4 proved every acceptance criterion individually — full end-to-end orchestration coverage comes from the existing `test_cli_multi_source.py` unit test and will be hardened in Session 5 via per-source staleness alerts on the `/health` endpoint.

**Context:** Reddit closed self-service API access in November 2025 under the "Responsible Builder Policy." New API approvals take weeks and are not guaranteed for personal monitoring tools. RSS feeds remain available without authentication and provide sufficient coverage for our use case, at the cost of narrower backfill depth and no comment coverage.

**Scope as built:**

Phase A — Reddit RSS refactor:
- Renamed `sources/reddit.py` → `sources/reddit_api.py` (dormant, history preserved)
- New `sources/reddit.py` uses httpx + feedparser against per-subreddit search RSS, with a polite User-Agent (`social-surveyor/<version> (by /u/<username>)`) and a per-instance throttle
- `reddit_username` added as a required config field; `min_seconds_between_requests` optional (default tuned live to 6s — see Phase B findings)
- `backfill` is best-effort against whatever RSS returns; logs `backfill.window_narrower_than_requested` when the oldest item is newer than the requested window
- Tests use httpx MockTransport with a scaffolded Atom fixture in `tests/fixtures/reddit_search_devops.xml`

Phase B — live verification fixed three real-world bugs:

1. **HN bodies leaked HTML entities and inline tags** (`&#x27;`, `<p>`, `<a>`). Algolia returns `comment_text` and `story_text` with markup intact; we now `_strip_html` both title and body before storage. Fixture tests use clean strings so only live data exposed this.

2. **Reddit's unauthenticated bucket is ~100 requests per ~10 minutes.** The 2s throttle burned it in ~200s and every subsequent request 429'd. Fix: default bumped to 6s (matches the bucket rate) plus explicit `x-ratelimit-reset` handling — on a 429 the source sleeps for the header value (capped at 900s) before letting tenacity retry, since tenacity's 30s ceiling can't recover from a ~5-minute reset. Verified live: the re-poll's first request 429'd (leftover from the earlier burst), slept 156s, and completed the rest of the poll with zero further 429s.

3. **`github.comments.cap_reached` fired per-issue-after-cap** (~40 spurious warnings per poll). Now guarded by a `_cap_warning_logged` flag; logs exactly once per poll.

Final DB after Phase B (ready for hand-labeling in Session 3):

| source      | rows |
|-------------|------|
| github      | 772  |
| hackernews  | 536  |
| reddit      | 120  |
| x           | 105  |
| **total**   | **1,533** |

X spend during Phase B: **106 reads × $0.005 = $0.53**.

**Acceptance (all met except as noted):**
- [x] `social-surveyor poll --project opendata --source reddit` works with no Reddit API credentials
- [x] ~~Multi-source `poll` without `--source`~~ covered by unit tests; live multi-source skipped (see Status above)
- [x] `data/opendata.db` has material rows across all four sources (target 500+; got 1,533)
- [x] HN, GitHub, X cursor behavior confirmed via back-to-back polls returning 0 new items
- [x] `usage --source x` reports correct totals
- [x] `--dry-run` for X confirmed to make zero API calls live (belt-and-suspenders on top of the unit test)
- [x] PLAN.md updated with new open questions (see Open Questions below)
- [ ] Reddit Responsible Builder request filed — *operator task, not coded; tracked as an open question*

**Non-goals:** New sources, classifier work, Slack integration. This session is cleanup and proof.

### Session 2.75 — Triage, labeling, and setup tooling

**Context:** Live verification (Session 2.5) gave us real items in the DB across four sources. Before the classifier can be built meaningfully, two manual activities need tooling:

1. **Query triage.** Deciding which queries and subreddits are producing signal versus noise, and iterating on the source YAML configs based on what we see. Without tooling this is tedious: eyeball SQLite, switch to YAML, re-poll, eyeball again.
2. **Labeling.** Producing ground-truth category + urgency labels for items, to serve as the eval set for the classifier in Session 3. Without tooling this is slow and error-prone (hand-edited JSONL leads to inconsistencies).

This session also introduces a first-run setup wizard that captures required credentials and project inputs interactively, which lets a fresh forker go from `git clone` to "data flowing" in a few minutes.

**Scope:**

- `social-surveyor setup --project <n>`: interactive wizard that captures Reddit username, GitHub token, X bearer token, and any other per-source config values, writes them to `.env`, and validates each by making a cheap live call (GitHub `/rate_limit`, Reddit RSS fetch). X and Anthropic tokens are syntactic-checked only — live validation would cost money and "every setup run makes a call" is the wrong pattern to bake in. Masked defaults (first 4 + last 3 chars) with Enter-to-keep semantics.
- `social-surveyor triage --project <n> [--source <n>] [--limit N]`: walks through recent items grouped by the query/config entry that produced them. For each group, shows a sample and prompts for a `keep / drop / refine / skip` decision. Decisions are written to a Markdown triage report with YAML-diff suggestions; the tool does not edit source configs automatically.
- `social-surveyor label --project <n> [--source <n>] [--resume]`: walks through unlabeled items, shows each one, prompts for category + urgency + optional note, appends each label to `projects/<n>/evals/labeled.jsonl` immediately so Ctrl-C loses at most one label.
- `social-surveyor stats --project <n>`: prints item counts by source, query, day, and label status. Explicitly surfaces the `(unknown query)` bucket for pre-`group_key` items.
- `projects/<n>/categories.yaml`: minimal category + urgency-scale definition that the labeling tool reads. Session 3's `classifier.yaml` will extend this; categories defined here become the classifier's categories with no duplication.

**Supporting work:**

- All four sources populate `raw_json.group_key` on newly fetched items so `stats`/`triage` can group by configured query. Existing 1,533 items stay in `(unknown query)` — honest attribution beats guessed attribution, and the bucket drains naturally as new items come in.
- Label file entries use the canonical `item_id` form `"{source}:{platform_id}"` (e.g., `"hackernews:41234567"`). Session 3's classifier joins on the same key.

**Acceptance:**

- Fresh clone + `uv sync` + `social-surveyor setup --project opendata` walks a new user through credential capture end-to-end without making any paid live calls.
- `social-surveyor stats --project opendata` gives a one-screen summary of the DB, including an explicit `(unknown query)` line.
- `social-surveyor triage --project opendata` shows query-grouped items, accepts k/d/r/s/v/q decisions, writes a YAML-diff suggestion to `projects/opendata/triage_YYYYMMDD_HHMM.md`.
- `social-surveyor label --project opendata` walks items one at a time, writes labels incrementally (crash-safe), supports `--resume` (default), and allows a one-step `b`ack.
- `projects/opendata/categories.yaml` exists with an initial category list + urgency scale.
- `projects/opendata/evals/labeled.jsonl` has 100+ entries after the between-session labeling work (operator responsibility, not Claude Code's).

**Non-goals:** The classifier itself (Session 3). The labeling tool is deliberately category-aware but classifier-agnostic — it doesn't call the LLM. No TUI framework (Rich/Textual); plain prompts are sufficient. No auto-editing of source YAML based on triage decisions — that's a footgun; the operator applies suggested edits manually.

### Session 3 — Classifier + eval harness

**Status:** Complete, shipped. Active prompt is v3 against a 143-item eval set (GitHub removed from opendata during iteration; source module preserved for future projects). Final metrics: 61.5% overall accuracy, alert-worthy precision 0.67 / recall 0.23, high-urgency MAE 4.43. Iteration arc: v1 baseline 47.7% → v2 structural rules 57.0% → taxonomy extension with `active_practitioner` + relabel pass → v3 teaches `active_practitioner` (61.5%). A post-ship v3.5 urgency-calibration attempt was reverted — it didn't move either ship criterion (recall stayed 0.23, high-urgency MAE went up 0.20). All acceptance criteria met except the aspirational alert-worthy P/R ≥ 0.75 targets; low recall is the open gap, deferred to post-Session-4 once production use provides more signal on what the classifier actually misses in practice.

**This session has a sub-plan.** The classifier prompt is the core product and deserves more ceremony than "write prompt, call API."

**Scope:** Haiku integration, classifier module, initial prompt v1, eval harness. Labels and categories exist from Session 2.75.

**Acceptance:**
- `classifier.yaml` schema supports categories, urgency scale, ICP description, prompt version
- `projects/<n>/classifier.yaml` extends the categories defined in `categories.yaml` rather than redefining them (single source of truth for the taxonomy)
- `social-surveyor classify --project <n> --item-id <id>` classifies a single item and prints the result
- `social-surveyor classify --project <n>` classifies all unclassified items in the DB
- Every classification is persisted with `prompt_version`
- Haiku API calls log input/output token counts for cost tracking
- Failed classifications retry once with backoff, then flag the item for manual review
- `social-surveyor eval --project <n>` runs the current classifier against all labeled items and prints per-category precision/recall + urgency MAE + a diff report showing every disagreement
- Eval completes in <30 seconds (important for fast iteration)
- The user can change `classifier.yaml`, re-run eval, and see the delta

**Sub-plan for the prompt itself:**

1. First pass: write a v1 prompt from the ICP description and category list in classifier.yaml. Don't over-engineer.
2. Run eval against the labels produced in Session 2.75. Expect ~70% accuracy.
3. Read the diff report. Identify systematic failure modes (e.g., "classifying tutorial posts as complaints").
4. Update the prompt with explicit negative examples for those failure modes. Bump to v2.
5. Re-run eval. Target 85%+ precision on alert-worthy categories.
6. Commit the prompt version that ships to prod. Keep v1 around for A/B.

**Source-specific context the classifier must read** (item shapes as of session 2; Reddit's will firm up in 2.5):

- **Reddit:** `title` + `body` (self-text posts). The subreddit is a useful prior — a complaint in `r/devops` reads differently than in `r/learnprogramming`. Check `raw_json` in the current `sources/reddit.py` for the exact field name (differs between the RSS and PRAW backends).
- **Hacker News:** story items have `title`; comment items have a synthesized title and the actual signal in `body`. `raw_json._tags` distinguishes them.
- **GitHub issues:** `body` is the issue body. `raw_json.is_pr` flags PRs if `type: both` is in config.
- **GitHub comments:** `body` is the comment; the meaningful context (issue title, state, repo) is in `raw_json.parent_issue`. **The classifier must receive both** — a neutral issue title can hide a high-signal comment. `raw_json.matched_query` names the query that surfaced it.
- **X:** `body` is the tweet text. `raw_json.tweet.public_metrics` (likes/replies/retweets) is a candidate urgency signal; `raw_json.author.verified` is a candidate reach signal.

**Also relevant to session 3's cost tracking:** the `api_usage` table already exists and tracks X reads. Extend it or add a `classifier_usage` table for Haiku input/output token counts — same shape, different `source` value (`"classifier"`).

**Non-goals:** Slack alerts, digest, routing (those come in session 4).

**Lessons from Session 3 iteration (v1 → v2 → v2a → v3):**

- Eval metrics measure the joint system of (classifier + labels + taxonomy). When metrics regress unexpectedly, any of the three could be the cause; don't assume the classifier is wrong until you've read the actual disagreement text.
- Adding a category to the taxonomy requires relabeling items across all source categories that plausibly contain migrants, not just the obvious donor category. This happened twice in Session 3 and is now a Convention.
- Prompt density has diminishing returns. The v2→v3 rule expansion (~1200→~2200 input tokens per call) improved some categories but created a magnet effect (Rule 3a read first + broadly framed → catchall behavior). Future prompt iterations should consider total length as a cost, not just rule correctness.
- Preregistered predictions aren't about accuracy — magnitude predictions missed consistently — but about surfacing mental models of what the prompt change should do. Disagreements between predicted and actual outcomes are more informative than the numbers themselves.

### Session 4 — Routing, alerts, digest, correction workflow

**Status:** Complete. Shipped Slack immediate alerts + daily digest + a
three-command correction loop (`label --item-id`, `silence`, `ingest`).
Append-only `labeled.jsonl` with latest-wins resolution landed inside
this session (previously deferred). APScheduler-backed `run` command
ties poll/classify/route together with a daily digest cron.

**Acceptance — met:**
- `routing.yaml` schema with pydantic validation + cross-file category id check
- Slack Block Kit immediate alerts (category-colored attachment) + digest with fixed category ordering and top-5-per-category overflow hint
- `silence` subcommand replaces the planned `handled` — non-teaching filter on the router, distinct from `label --item-id` which is classifier feedback
- `digest` with `--dry-run`, `--category` (stdout inspection), `--since` (retrospective)
- `--dry-run` on `route` and `digest`; `poll` and `classify` already had it
- Every Slack alert ends with copy-paste correction lines including item id, category placeholder, urgency placeholder, and a silence variant

**Acceptance — scope-shifted:**
- `explain --item-id` was pre-existing from Session 3 — not re-touched
- Cost kill-switch: `cost_caps` parsed into `RoutingConfig`, enforcement deferred. X source already enforces its own `daily_read_cap`; Haiku-tokens enforcement is a small follow-on that doesn't warrant blocking Session 4 alongside the Session 5 infra work.

**Key design decisions captured:**
- **Path A (no threading) over Path B (Slack app + bot token).** Incoming webhooks return only `ok` — no message `ts`, no threaded replies. Rather than upgrading to a bot-token app, the digest shows top-5 items per category inline with an overflow hint pointing at `sv digest --category <cat>` for the full stdout listing. Better visual consistency day-to-day (main message is the same shape regardless of volume); the builder functions stay pure so a future upgrade to `chat.postMessage` is surface-level.
- **`silence` keyed on `item_id`, not `classification_id`.** Silence persists across prompt-version reclassifications. If a new classification under v4 disagrees meaningfully with the silence decision under v3, the user can re-ingest and re-evaluate explicitly — the alternative (surfacing automatically on any new classification) is worse UX than a 10-second `sv ingest` when you want to reconsider.
- **Alerted-earlier items do NOT duplicate in category sections.** "N items across M categories · K alerted earlier" in the digest header lifts alerted items into their own section; they don't render twice. Reduces the user's attention surface.
- **`silence` has no reversal command.** Mis-silences are expected to be 1-in-20 of 1-2 silences per week — roughly once every 3-5 months. `sqlite3 DELETE` is sufficient at that frequency. The recovery SQL is advertised in the `silence` help text and the README.

**Non-goals honored:** EC2 deployment (Session 5), eval-set expansion, classifier v4, multi-project testing (opendata only), Slack interactive buttons.

**Follow-on notes for future sessions:**
- **Path B upgrade to a bot-token Slack app.** If daily use shows the `sv digest --category` inspection command is friction, graduate to a Slack app with a bot token so `chat.postMessage` can thread collapsed-category detail off the main message. The builder functions stay pure; only `post_to_slack` changes surface.
- **Haiku-tokens cost cap enforcement.** The `cost_caps.daily_haiku_tokens` field is parsed but not yet enforced. Add a pre-classify check in `classifier.py` that sums today's `api_usage` tokens and halts with an infra alert if exceeded. Trivial once needed; deferred until we see a runaway.
- **`alert_worthy_categories` is already in `routing.yaml`**, closing the hardcoded-in-eval-harness item from Open Questions.

Session 4 deferred: `deploy.sh` SSM automation, `secrets.resolve_secret` SSM fallback, Haiku token cost cap enforcement. Rolled forward into Session 5a-polish.

### Session 5a — Minimal production deploy

**Scope:** narrowed for time-to-first-digest (aspirational 9am Pacific Monday). Get the service running on EC2 with a daily digest firing; defer every polish item that isn't on that critical path.

**What ships in 5a:**
- Pulumi program in `deploy/pulumi/` (Python-based, Pulumi Cloud state backend)
- `Pulumi.opendata.example.yaml` committed as a template; `Pulumi.opendata.yaml` gitignored
- Minimal Pulumi program: EC2 instance (t4g.micro, existing VPC/subnet — Claude Code inspects AWS and asks which), IAM role with SSM + SSM Parameter Store read, security group allowing outbound HTTPS, EBS gp3 volume (SQLite data)
- Bootstrap script `deploy/bootstrap-ec2.sh` — installs uv, creates `social-surveyor` user, sets up directories, installs systemd unit
- systemd template unit `deploy/social-surveyor@.service` — runs `social-surveyor run --project <instance-name>`
- Manual secrets seeding via `aws ssm put-parameter` — one-time, documented step
- Manual first deploy: rsync repo to instance, uv sync, systemctl enable+start — scripted as a short runbook but no `deploy.sh` automation yet
- Service running; digest cron configured to fire 9am Pacific daily

**Explicitly deferred from 5a** (5a-polish items, not blockers):
- `deploy.sh` automated rsync deploy
- `secrets.resolve_secret` SSM integration (5a uses bootstrap-exported env vars as a shortcut)
- EBS snapshot lifecycle policy
- Haiku token cost caps enforcement (still deferred from Session 4; carries forward)
- Pulumi stack config polish for forker-friendliness

**Non-goals** (later sessions, not 5a-polish):
- GitHub Actions CI/CD (Session 5b)
- Health endpoint + infra alerts (Session 5c)
- CloudWatch Logs, custom metrics dashboards, multi-region, auto-scaling

### Session 5a-polish — Complete the deferred items

**Status:** Complete, shipped 2026-04-20 across PRs #7, #8, #9, #10. All
four deferred items landed; `Pulumi.opendata.example.yaml` polish
deferred as a trivial follow-up (see below).

**Acceptance — met:**
- [x] Haiku token cost cap enforcement in `cost_caps.py` — UTC-day
  sum of input+output tokens; halts classification + pages infra
  channel (exactly once per day via `infra_alerts` idempotency table);
  warns at 80% of cap. `opendata` cap set to 2.2M tokens, derived
  from first post-deploy 24h (~1.1M, backfill-inflated) doubled —
  revisit downward once steady-state is observed.
- [x] `deploy/deploy.sh` — accepts tag, branch, or commit SHA;
  defaults to `origin/main` HEAD; resolves locally to a SHA so the
  remote checkout matches the laptop's view. `--dry-run` / `--dirty`
  / `--project` flags. Tests cover all four invocation shapes.
- [x] `secrets.resolve_secret` SSM fallback — env-first unchanged,
  SSM opt-in via `SOCIAL_SURVEYOR_SSM_PREFIX` env var, per-process
  cache keyed on `(prefix, name)`. Systemd unit sets the prefix on
  the instance. `boto3` added as a dependency.
- [x] EBS snapshot lifecycle — DLM policy fires weekly Sunday 02:00
  UTC, retains 4 snapshots. Root volume tagged `Snapshot=<project>`
  so multi-project accounts stay scoped. Manual restore outline in
  `deploy/README.md`.

**Acceptance — met in docs follow-up (PR #11):**
- [x] `Pulumi.opendata.example.yaml` polish — expanded per-field
  comments cover what each value drives (project_name identity,
  subnet reachability from SSM, SSM prefix ↔ secrets fallback,
  keypair opt-in), plus a stack-naming convention note.

**Runtime gotchas encountered (documented to save future debugging):**
- SSM's `AWS-RunShellScript` runs commands under `/bin/sh` (dash on
  Ubuntu), not bash. Scripts using `set -o pipefail` or other
  bash-isms need to be wrapped in `bash -c`. `deploy.sh` uses a
  base64 → bash pattern; surfaced on first live deploy and fixed in
  PR #10.
- Pulumi `aws.dlm.LifecyclePolicy` Python SDK (v6.83) rejects
  `interval_unit="WEEKS"` (only HOURS) and types `create_rule.times`
  as a single str despite the docs suggesting a list. Use a cron
  expression (`cron(0 2 ? * SUN *)`) and typed Args classes rather
  than raw dicts. PR #8.

**Follow-ons (not blocking):**
- **Revisit Haiku cap value.** 2.2M is intentionally loose to avoid
  false-halt on backfills. After ~1 week of non-backfill operation,
  tune down to whatever steady-state + 2x buffer suggests (projected:
  200-500k range).
- **`social-surveyor-load-env` is now redundant.** The SSM fallback
  in `resolve_secret` makes the env-file marshaling unnecessary, but
  the bootstrap script and systemd unit still use it. Belt-and-
  suspenders is fine for now; remove in a session that already
  touches bootstrap/systemd to avoid a single-purpose change.
- **Infra Slack channel.** `OPENDATA_SLACK_WEBHOOK_INFRA` env is
  optional; when unset, cost-cap alerts land in the immediate
  channel with a `[INFRA]` prefix. Operator can create a dedicated
  channel + webhook later and seed into SSM (no code change needed).

### Session 5b — CI/CD

Runs after 5a-polish stabilizes. Expected duration: 1-2 hours.

- GitHub Actions workflow on push to `main`
- Lints, tests, SSH-deploys to EC2 via deploy key stored in GitHub Secrets
- Rollback story: revert commit, push, auto-deploy

### Session 5c — Monitoring and observability

Added session — explicitly addresses "how do I know this is running and not silently failing?" Original Session 5 bundled this in; pulling out for focus.

- `/health` HTTP endpoint on localhost:8080 (stdlib `http.server`): last poll per source, DB row counts, last digest send, daily cost, start time
- Infra alerts Slack webhook (separate from business webhook — `#social-monitoring-infra` or similar)
- Two alert conditions POSTed to infra channel:
  - Any source not polled in >30 minutes
  - Classification error rate >5% in the last hour
- Implemented inside the existing `run` process via APScheduler (no new long-running process)
- journald retention / rotation settings for the systemd unit
- README "how to check if it's running" section with SSM port-forward example for hitting `/health`

**Timing:** after a week or so of production use, when real failure modes have surfaced. Premature to build before you know what actually goes wrong.

**Non-goals (across all 5x sessions):** RSS blog sources, author enrichment, multi-region.

### Session 5d — Digest de-duplication + inspection flexibility

**Bug surfaced:** Observing the prod dry-run output on 2026-04-20 revealed that the digest was re-shipping items from the previous cycle. Root cause in `storage.list_alerts_in_window`: for `include_unsent=True` (the digest-render path), the SQL matched both pending alerts AND any already-sent alert whose `sent_at` fell within the rolling window. With a 24h cadence and 24h window, every digest-channel item shipped in two consecutive daily digests — the stated contract in the CLI's docstring was "tomorrow's run doesn't re-include," but the SQL did not enforce it.

**Secondary concern:** the "🔔 Alerted earlier today" digest section duplicated content already delivered to the immediate Slack channel. Once consumed there, a recap in the digest was noise rather than signal.

**Scope:**

- Narrow the `include_unsent=True` branch in `storage.list_alerts_in_window` to return only `sent_at IS NULL AND queued_at >= since` (pending only, disjoint from the sent-only branch).
- Remove the "Alerted earlier today" section from `build_digest` — drop `NotifierItem.alerted_at`, the `_alerted_earlier_block` helper, the alerted count in the top header, and `ALERTED_EARLIER_CAP`.
- `_run_category_inspection` now issues three window queries (sent immediate + sent digest + pending digest) and concatenates, annotating each line with `(sent)` or `(pending)` so the operator can see full local history regardless of which Slack channel the item went to.

**Acceptance:**

- `tests/test_storage.py::test_list_alerts_in_window_consecutive_digest_cycles_do_not_duplicate` passes — pinpoint regression for the SQL OR-branch bug.
- `tests/test_cli_digest.py::test_digest_does_not_re_include_items_from_previous_cycle` passes — end-to-end proof: first digest ships item A, second digest (same window) ships zero items.
- `tests/test_cli_digest.py::test_digest_category_inspection_shows_both_sent_and_pending` passes — inspection mode surfaces delivered + pending with state tags.
- `tests/test_notifier.py::test_digest_has_no_alerted_earlier_section` passes — no recap, no "alerted" count in the header.
- Full suite: 376 green.
- Prod dry-run via `social-surveyor digest --project opendata --dry-run` shows a strict delta from the previous cycle (verify by running dry-run, confirming items shipped in today's 9am digest are absent).

**Non-goals:** Changing the `include_unsent` API into an enum or three-valued parameter; introducing a "delivered-at" audit view beyond what category inspection already surfaces.

### Session 5.5 — Public/private config split (conditional)

**Trigger:** If by the end of session 5 the classifier prompt and ICP description reveal competitive positioning you'd rather not telegraph publicly, do this session. Otherwise skip.

**Scope:** Split sensitive config fields into `projects/<n>/private.yaml`, update config loader to merge, update `.gitignore`, document the pattern.

**Acceptance:**
- `projects/<n>/private.yaml` is in `.gitignore`
- `projects/<n>/private.example.yaml` committed, showing structure
- Config loader merges `classifier.yaml` + `private.yaml`, with private overriding public
- README documents the pattern for forkers who want to do the same
- Existing configs migrated without behavior change (eval set still passes)
- Evals labeled.jsonl optionally moves to gitignored location (user's call at this point)

**Non-goals:** Everything else.

### Session 6 — Blog RSS sources

**Scope:** Generic RSS source module covering blog platforms (Medium tags, Dev.to tags, Lobste.rs, individual Substacks). Builds directly on the feedparser infrastructure introduced in Session 2.5 for Reddit RSS — much less new code than originally planned.

**Acceptance:**
- `rss.yaml` schema: list of feed URLs with optional per-feed keyword filters and per-feed poll cadence
- `social-surveyor poll --project <n> --source rss` works
- Feed parser handles malformed XML gracefully (feedparser does most of this already)
- Per-feed polling cadence (hourly by default, configurable)
- Same dedupe, classify, route path as other sources

**Non-goals:** Author enrichment.

### Session 7 — Author enrichment

**Scope:** Given a high-urgency classification, resolve the author's X and LinkedIn handles, attach to the Slack alert.

**Acceptance:**
- New `enrichment.py` module, runs async after classification for items with urgency >= threshold
- For Medium/Substack/Dev.to posts: parse author bio links, extract X and LinkedIn handles
- For Reddit/HN: attempt to correlate username with X via public search (best-effort, often fails — that's fine)
- For X posts: the author is already known
- Optional: search for the author's own social share of their blog post, link to that thread
- Enriched handles appear in Slack alerts; failures don't block alerts
- Enrichment failures logged but not alerted

**Non-goals:** LinkedIn scraping (too fragile; handles only, no post fetching).

---

## Conventions

- **Python 3.12.** Type hints everywhere. `from __future__ import annotations` in every module.
- **Package manager: uv.** `uv.lock` committed. Never use pip directly.
- **Lint/format: ruff.** Single tool for both. Config in `pyproject.toml`. Enforced in CI.
- **Tests: pytest.** Mocks for external APIs. No live API calls in CI. Integration tests with `--live` flag for local use only.
- **Config validation: pydantic v2.** Every YAML loaded into a typed model. Fail fast on bad config.
- **Logging: structlog, JSON output.** Every log line includes `project`, `stage`, `source`, `item_id` where relevant.
- **CLI: Typer.** One entry point (`social-surveyor`), subcommands per action. All subcommands require `--project`.
- **Commit style: conventional commits.** `feat:`, `fix:`, `chore:`, `docs:`. Single-purpose commits.
- **PR-per-session workflow.** Each session is one PR. PLAN.md updated in the same PR if plan evolves.
- **No ORM.** Raw SQL against SQLite via `sqlite3` stdlib.
- **No web framework.** The `/health` endpoint is a stdlib `http.server`, nothing fancier.
- **No Docker.** systemd + virtualenv + uv on the EC2 host. Simpler, smaller, faster.
- **Live verification is required for "complete."** Every source module must be verified against real APIs within 7 days of being written, or it does not count as "complete" in session acceptance. The session that introduces the source owns verification; if blocked (credentials, API policy, etc.), the next session must include the verification step as a prerequisite.
- **Mocked tests do not satisfy live-API acceptance criteria.** Acceptance criteria involving live API calls are satisfied only when verified by the user with real credentials, not by mocked tests. Session PR descriptions must explicitly flag which criteria were verified live vs mock-only.
- **Labels and classifier prompts are separately authored.** The labeling process (Session 2.75) produces ground-truth labels against an agreed category taxonomy. The classifier prompt (Session 3) is written to match the same taxonomy. When they disagree, treat it as a potential prompt bug or a potential label inconsistency, not automatically either one.
- **Taxonomy changes trigger a full relabel pass across all potentially-affected source categories.** When a new category is added to `categories.yaml`, items that might migrate to it are not only in the obvious donor category. Walking only the donor category will leave stale labels in other categories and will cause the next eval run to misattribute classifier improvements as classifier regressions. Run `--reconsider` on every category that plausibly contains items for the new category, not just the one that seems most related. For opendata's `active_practitioner` addition, the full pass should have covered `self_host_intent`, `neutral_discussion`, and `off_topic` at minimum.

---

## Open questions

Updated as sessions reveal new decisions. Current open items:

- **Reddit API approval via Responsible Builder Policy.** Filed on `<DATE TO BE FILLED>`, status TBD. When/if approved, switch `sources/reddit.py` to use the preserved PRAW-based `sources/reddit_api.py` for better comment coverage and deeper backfill. Until then, RSS is the source of truth for Reddit.
- **Reddit comment coverage gap.** RSS gives us posts only, no comments. For devops/SRE discussion specifically, comment-level signal is often richer than post-level. If the classifier shows meaningful misses traceable to this gap, consider per-post comment RSS fetches (expensive) or wait for API approval.
- **Reddit RSS bucket rate and throttle tuning.** Observed live in Session 2.5 Phase B: Reddit's unauthenticated bucket is ~100 requests per ~10 minutes. We default `min_seconds_between_requests` to 6.0s to stay under that, plus honor `x-ratelimit-reset` on any 429 that slips through. If the bucket size changes (Reddit has been tightening throttles periodically), we'll see it as `reddit.rate_limited.backing_off` warnings in logs — first check whether the default should move again.
- **GitHub dry-run rate-limit consumption.** Because dry-run still performs comment fetches, it eats from the same 5k/hr search budget. Watch this during Session 2.5 live testing; if it causes pain, add a `max_comments_in_dry_run` knob or subsample.
- **GitHub comment matching precision.** Best-effort substring, case-insensitive, stopwords dropped. The `github.comments.fetched` log line with `comments_total` vs `comments_matched` is the diagnostic. Revisit if precision suffers during Session 3 eval.
- **X full-archive search (`/2/tweets/search/all`).** Different tier, different pricing. Backfill clamped to Recent Search's 7-day window. Revisit in Session 5+ once we see what 7 days of X tweets does for the classifier.
- **Schema migrations.** We now have `items`, `source_cursors`, `api_usage`. Sessions 3 and 4 add `classifications` and `alerts` respectively — both additive, fine under `CREATE TABLE IF NOT EXISTS`. The first structural change to an existing column will need a proper migration story; defer building that framework until we need it (probably never in MVP).
- **X API authentication flow in pay-per-use:** resolved in Session 2.5 Phase B. Pay-per-use works on apurva's account with a standard bearer token; 106 reads total during verification for $0.53. The `daily_read_cap` pre-call check, `api_usage` ledger, and `usage` CLI all behaved as designed live. No fallback to the legacy Basic tier needed.
- **Slack interactive buttons vs CLI-only handled:** the Slack button approach requires exposing an HTTPS endpoint to Slack, which complicates the EC2 SG setup. CLI-only (`social-surveyor handled <id>`) is the MVP choice. Revisit after Session 5 if the CLI workflow feels clunky in daily use.
- **Rate limit for Anthropic API calls:** during backfill operations we could hit Anthropic's tier-based rate limits. Need concurrency control in `classifier.py` — single worker with configurable RPS cap is simplest.
- **Eval set size:** 50 items is the starting target; real accuracy tuning may need 200+. Budget time in Session 3 for hand-labeling to keep pace.
- **Multi-project conflicts:** if two projects poll the same subreddit, we fetch and store twice. Acceptable for now (different projects have different classifiers, different storage). Revisit if it becomes a cost issue.
- **Refresh policy for mutable item metadata.** `upsert_item` is insert-if-new; we never refresh comment counts, scores, issue state, etc. Fine for classification (post content is immutable) but would be nice for digest annotations ("this post now has 230 upvotes, up from 40 when we first saw it"). Revisit in Session 4 or later.
- **Silent per-source failures.** Our multi-source poll catches exceptions and logs `poll.source.failed`, which covers crashing sources. It does *not* catch silent failures (a source that starts returning `[]` because an upstream API schema changed, or a hang where no timeout fires). Session 5's `/health` endpoint will track "last successful poll per source per project" so staleness alerts catch these; until then, eyeball the per-source row counts periodically.
- **Category taxonomy stability.** Categories defined in Session 2.75's `categories.yaml` will be used by both the labeler and (in Session 3) the classifier. Renaming or restructuring categories after labeling has started invalidates prior labels. Treat the taxonomy as a decision to be made deliberately early and then held stable; if it must change, there should be a migration step in the change. **Note from Session 3 iteration:** even adding a new category (without renaming existing ones) requires a full relabel pass across all potentially-affected source categories, not just the obvious donor. Label drift across untouched categories will show up as apparent classifier regressions in subsequent evals. See the "Taxonomy changes trigger a full relabel pass" convention.
- **Deployment identifiers in git history.** `Pulumi.opendata.example.yaml` is committed as a template with placeholder values; the real `Pulumi.opendata.yaml` with VPC/subnet IDs is gitignored. This is the conservative choice for a public repo. If the project ever moves to private or a multi-deployer model, revisit — committing real values makes forking easier at the cost of revealing infrastructure identifiers.
- **Haiku daily cost cap value.** Set to 2,200,000 tokens in Session 5a-polish, derived from the first post-deploy 24h (which was dominated by an initial backfill of ~440 items). Steady-state is projected at <100k tokens/day, so the cap has ~20x headroom. After a week of non-backfill operation, tune downward to something closer to (observed steady-state) × 2 — likely 200-500k. Revisit the value alongside whatever session is next touching `routing.yaml`.
- **Infra Slack channel.** `routing.infra.webhook_secret` is optional; if unset, cost-cap alerts land in the immediate channel with a `[INFRA]` prefix. Operator can create `#social-surveyor-infra` (or similar) in Slack, seed the webhook into SSM as `OPENDATA_SLACK_WEBHOOK_INFRA`, and the cost-cap path picks it up on the next classify tick with no code change. Also relevant for Session 5c's staleness alerts.
- **`social-surveyor-load-env` redundancy.** Session 5a-polish's SSM fallback in `resolve_secret` makes the bootstrap's env-file marshaling unnecessary. The helper is still installed and the systemd unit still sources `/etc/social-surveyor/%i.env` — kept as belt-and-suspenders. Remove in a future session that already touches `deploy/bootstrap-ec2.sh` or `deploy/social-surveyor@.service` to avoid a single-purpose cleanup commit.

---

## Production labeling refinement (deferred)

Once the classifier ships and social-surveyor is in daily use, the labeling workflow shifts from "initial bulk labeling" to "correct individual mistakes as they surface." The current Session 2.75 tool is optimized for the former; these additions serve the latter. All of these are deferred until we feel real friction in daily use, or until Session 4 brings them in naturally alongside Slack integration.

**Append-only `labeled.jsonl` with timestamp precedence.** Labels should be append-only with `labeled_at` timestamps. When the eval harness reads the file, it groups by `item_id` and takes the entry with the latest `labeled_at` as the effective label. Earlier entries are retained for history/audit but don't affect eval scoring. This lets re-labeling be a simple append operation rather than a destructive edit, preserves change history, and makes Ctrl-C always safe.

*State as of Session 2.75:* the write path already appends and every entry already carries `labeled_at`, so no data migration is needed. What's **not** done is the read path: `labeling.labeled_ids()` collapses to a set of item_ids with no "latest wins" logic, and `pop_last_label()` — the one-step `b`ack command — destructively truncates the last line. Moving to pure timestamp-precedence means two small code changes:

- Rewrite `labeled_ids` / add a `resolve_effective_labels()` helper that groups by `item_id` and returns the entry with max `labeled_at` per group (used by eval + the queue builder).
- Replace `pop_last_label` with an append-a-correction flow (or drop the in-loop `b`ack entirely once `--item-id` relabeling exists).

**`label --item-id <id>`.** Label one specific item by its canonical ID (`{source}:{platform_id}`), bypassing the walk-through. If the item is already labeled, show the current label and prompt to confirm before appending a new (relabel) entry. This is the critical friction-killer for the "wrong alert just landed in Slack" workflow.

**`label --relabel [--category <c>] [--source <n>]`.** Walk through already-labeled items instead of unlabeled ones. Useful after a taxonomy change, after the user's sense of a category shifts, or for spot-checking consistency across a category. Filters narrow the set.

**Slack alerts include copy-pasteable label commands.** Every Slack alert (built in Session 4) should include at the bottom:

```
To correct this classification:
social-surveyor label --project <n> --item-id <source>:<platform_id>
```

This makes the "see wrong alert → label correctly" loop a 30-second operation: copy command, paste in terminal, label, done. Without this, the loop requires the user to remember the item ID, find the right command, and type it — high friction, predictably skipped, eval set stagnates.

**Expected timing.** Session 4 (Slack + routing) is the natural home for the copy-pasteable command, and we should do the `--item-id` and `--relabel` additions alongside it. The read-path timestamp-precedence change should land whenever we first notice label overwriting is happening, or proactively in Session 4 alongside the `--item-id` path — cheapest to change both at once.

---

## Non-goals (for the foreseeable future)

Things we are explicitly *not* building, so scope creep doesn't eat the project:

- A web UI of any kind (configs are files, results are Slack messages)
- Multi-user auth (this is single-operator software)
- A SaaS version or hosted offering (open source self-hosted only)
- Kubernetes, Terraform, or other heavy infra — one EC2 box is the target
- A queue system (Kafka, SQS, Redis). SQLite + APScheduler handles the load at this scale.
- A vector database. Keyword prefilter + LLM classification is plenty.
- Support for languages other than English in the classifier (can be added later; v1 is English-only)
- Real-time Filtered Stream from X (pull-based Recent Search is simpler and sufficient)
- Automated response generation. The tool surfaces opportunities; the human engages. Intentional design choice.
- Devvit / on-Reddit apps. Wrong architectural shape — Devvit apps run on Reddit's servers for Reddit users, not as external daemons consuming Reddit data.
