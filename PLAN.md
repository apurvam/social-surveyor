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

### Session 4 — Routing, alerts, digest, handling

**Scope:** Slack integration, urgency-based routing, immediate alerts, daily digest, mark-as-handled loop, explain command.

- Slack alerts include a copy-pasteable `social-surveyor label --item-id <id>` command as the last line of each alert, so users can correct misclassifications without having to remember item IDs or construct the command manually. See the "Production labeling refinement (deferred)" section for the full rationale.

**Acceptance:**
- `routing.yaml` schema: immediate threshold, digest time/timezone, channel secret references
- Slack alerts use Block Kit: title, source, urgency, category, reasoning, URL
- `social-surveyor handled <item_id>` marks an item as handled; handled items never re-alert even if re-matched
- `social-surveyor digest --project <n>` produces a formatted daily summary of unalerted classifications in the last 24h, grouped by category, sorted by urgency
- Digest includes daily cost summary (Haiku tokens + X API usage, if applicable)
- `social-surveyor explain --item-id <id>` prints: raw item, prefilter result, classifier input, classifier raw output, routing decision
- `--dry-run` flag on `poll`, `classify`, `digest` — does everything except send to Slack
- Cost kill-switch: daily caps on Haiku tokens and X reads in `routing.yaml`; pipeline halts and sends an infra alert if exceeded
- Every Slack alert ends with a copy-pasteable label command that works when pasted directly into the user's terminal.

**Non-goals:** EC2 deployment, CI/CD, author enrichment, Slack interactive buttons (CLI-only for handled).

### Session 5 — Deployment and observability

**Scope:** EC2 provisioning, systemd template unit, GitHub Actions CI/CD, SSM Parameter Store integration, structured logging, health endpoint.

**Acceptance:**
- `deploy/bootstrap-ec2.sh` provisions a fresh t4g.micro in us-east-1: user, directory structure, systemd units installed, IAM role attached for SSM read
- t4g.micro on 1-year Savings Plan (~$3.50/mo), 10 GB gp3 EBS, public subnet with no inbound SG rules, SSM Session Manager for access
- `deploy/social-surveyor@.service` runs the process under a dedicated `social-surveyor` user with `Restart=always`
- `.github/workflows/deploy.yml` on push to `main`: lints, tests, SSHes to EC2, runs `deploy/deploy.sh` (rsync code, uv sync, systemctl restart all enabled instances)
- Secrets in SSM at `/social-surveyor/<n>`, read via boto3, cached in-process
- structlog JSON output to stdout, captured by journald, optionally forwarded to CloudWatch
- `/health` endpoint on localhost:8080: last successful poll per source per project, DB row counts, last digest send, current daily cost
- Access `/health` from laptop via SSM port forwarding
- Infra alerts (poll failures, classification error rate >5%, cost cap hit) go to a separate Slack channel distinct from business alerts
- README has a "deploy your own" section that works end-to-end for a fresh forker

**Non-goals:** RSS blog sources, author enrichment, multi-region.

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
- alert_worthy should be a per-category config field, not hardcoded in the eval harness. Currently cost_complaint + self_host_intent + competitor_pain is hardcoded. When active_practitioner was added, we explicitly told the eval harness to exclude it. When the second project (agent-infra) starts, this hardcoding will break immediately. Factor out in the first session that starts agent-infra, or proactively in a small cleanup session.  

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
