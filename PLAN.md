# social-surveyor — Plan

A living plan document for `apurvam/social-surveyor`. Updated as sessions complete and decisions evolve.

---

## Product statement

social-surveyor is a self-hosted social listening pipeline for founders and small teams who want to monitor online conversations about their product space without paying $100–300/month for enterprise social listening tools (Octolens, Intently, Brandwatch, etc.). It polls a configurable set of sources (Reddit, Hacker News, X, GitHub, RSS feeds), dedupes and classifies matching posts with an LLM tuned to the user's specific ICP and competitive positioning, and routes results to Slack — high-urgency posts as immediate alerts, everything else as a daily digest.

The thesis is that a small Python process plus a well-tuned classifier prompt outperforms general-purpose social listening tools for a specific vertical, because the user owns the prompt and can iterate on it against real data in their domain. The cost is a few dollars a month of AWS infra, a few dollars of LLM API calls, and optional pay-per-use charges on the X API.

The project is designed to be forked. Each "project" (a monitoring configuration for a specific topic) lives under `projects/<name>/` with its own sources, classifier, routing, and eval set. The same engine runs multiple projects as separate systemd-managed processes. Initial use cases: monitoring for opendata-timeseries (observability cost complaints, durable Prometheus questions) and agent infrastructure market research.

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

- `sources/` — one module per platform, all implementing the same `Source` ABC with a `fetch(since_id) -> list[RawItem]` method. Swapping in a new platform is ~100 lines. Supported in MVP: Reddit, Hacker News, GitHub, X Recent Search. Added later: RSS.
- `storage.py` — SQLite access layer. Three tables: `items`, `classifications`, `alerts`. One database file per project.
- `classifier.py` — assembles the prompt from `classifier.yaml`, calls the Anthropic API, parses the JSON response. Stores `prompt_version` on each classification for regression tracking.
- `router.py` — reads classifications, decides immediate-vs-digest based on urgency threshold, deduplicates against the `alerts` table to avoid re-alerting on the same item.
- `notifier.py` — Slack webhook client. Block Kit formatting for rich alerts.
- `digest.py` — queries unsent classifications in the last 24h, formats a ranked summary, posts to the digest channel.
- `pipeline.py` — orchestrates a single pipeline run: fetch → dedupe → prefilter → classify → route → notify.
- `cli.py` — Typer-based CLI. Subcommands: `poll`, `classify`, `digest`, `backfill`, `explain`, `eval`, `handled`.
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
│   ├── storage.py
│   ├── classifier.py
│   ├── router.py
│   ├── notifier.py
│   ├── digest.py
│   ├── pipeline.py
│   ├── cli.py
│   ├── config.py
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

- CLI commands all take `--project <name>` (required).
- systemd template unit `social-surveyor@opendata.service` → runs `social-surveyor run --project opendata`.
- Adding a new project: create `projects/<name>/`, populate the four YAML files, `systemctl enable --now social-surveyor@<name>`. No code changes.
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

### Session 1 — Skeleton + real data collection

**Scope:** Project structure, config models, SQLite storage, working Reddit source, multi-project support, dry-run mode, Reddit backfill. Create `projects/opendata/` with real subreddit configs so data collection starts immediately.

**Acceptance:**
- `uv sync` installs all deps cleanly
- `social-surveyor --help` shows subcommands
- `social-surveyor poll --project example --source reddit --dry-run` fetches posts and prints to stdout without writing to DB
- `social-surveyor poll --project example --source reddit` (no dry-run) writes rows to `data/example.db`
- Re-running the poll does not create duplicate rows (dedupe by `(source, platform_id)`)
- `social-surveyor backfill --project opendata --source reddit --days 7` fetches historical posts from configured subreddits
- Pydantic config errors produce helpful messages, not stack traces
- `projects/example/sources/reddit.yaml` has a reasonable default config (generic devtool monitoring)
- `projects/opendata/sources/reddit.yaml` has apurva's real subreddits: devops, kubernetes, sre, selfhosted, PrometheusMonitoring, observability, with queries around observability cost complaints and Prometheus storage questions
- Basic pytest suite for storage dedupe and config loading
- After session: run backfill + a few polls, expect 500+ real Reddit posts in `data/opendata.db` ready for hand-labeling between sessions

**Non-goals:** LLM calls, Slack, digest, other sources, deployment.

### Session 2 — Remaining core sources

**Scope:** Hacker News (Algolia), GitHub (issues/comments search), X (Recent Search, pay-per-use). All implement the same `Source` ABC.

**Acceptance:**
- Each source has a config schema, a module, and tests
- `social-surveyor poll --project example --source hackernews` works
- Same for `github` and `x`
- `social-surveyor poll --project example` (no `--source`) polls all sources
- X source uses `since_id` for incremental fetches (cost control)
- GitHub and Reddit handle rate limits with exponential backoff
- RawItem schema is consistent across sources (id, source, url, title, body, author, created_at, raw_json)
- `projects/opendata/` has real configs for each new source

**Non-goals:** RSS (deferred to session 6), LLM, Slack.

### Session 3 — Classifier + eval harness

**This session has a sub-plan.** The classifier prompt is the core product and deserves more ceremony than "write prompt, call API."

**Scope:** Haiku integration, classifier module, initial prompt v1, eval harness, 50 hand-labeled items.

**Acceptance:**
- `classifier.yaml` schema supports categories, urgency scale, ICP description, prompt version
- `social-surveyor classify --project <name> --item-id <id>` classifies a single item and prints the result
- `social-surveyor classify --project <name>` classifies all unclassified items in the DB
- Every classification is persisted with `prompt_version`
- Haiku API calls log input/output token counts for cost tracking
- Failed classifications retry once with backoff, then flag the item for manual review
- `projects/opendata/evals/labeled.jsonl` contains 50+ hand-labeled items (user fills this in between sessions 2 and 3)
- `social-surveyor eval --project <name>` runs the current classifier against all labeled items and prints per-category precision/recall + urgency MAE + a diff report showing every disagreement
- Eval completes in <30 seconds (important for fast iteration)
- The user can change `classifier.yaml`, re-run eval, and see the delta

**Sub-plan for the prompt itself:**

1. First pass: write a v1 prompt from the ICP description and category list in classifier.yaml. Don't over-engineer.
2. Hand-label 50 items from the DB (mix of obvious and ambiguous).
3. Run eval. Expect ~70% accuracy.
4. Read the diff report. Identify systematic failure modes (e.g., "classifying tutorial posts as complaints").
5. Update the prompt with explicit negative examples for those failure modes. Bump to v2.
6. Re-run eval. Target 85%+ precision on alert-worthy categories.
7. Commit the prompt version that ships to prod. Keep v1 around for A/B.

**Non-goals:** Slack alerts, digest, routing (those come in session 4).

### Session 4 — Routing, alerts, digest, handling

**Scope:** Slack integration, urgency-based routing, immediate alerts, daily digest, mark-as-handled loop, explain command. Full backfill support (session 1 only did Reddit).

**Acceptance:**
- `routing.yaml` schema: immediate threshold, digest time/timezone, channel secret references
- Slack alerts use Block Kit: title, source, urgency, category, reasoning, URL
- `social-surveyor handled <item_id>` marks an item as handled; handled items never re-alert even if re-matched
- `social-surveyor digest --project <name>` produces a formatted daily summary of unalerted classifications in the last 24h, grouped by category, sorted by urgency
- Digest includes daily cost summary (Haiku tokens + X API usage, if applicable)
- `social-surveyor backfill --project <name> --source <any> --days N` works for all sources
- `social-surveyor explain --item-id <id>` prints: raw item, prefilter result, classifier input, classifier raw output, routing decision
- `--dry-run` flag on `poll`, `classify`, `digest` — does everything except send to Slack
- Cost kill-switch: daily caps on Haiku tokens and X reads in `routing.yaml`; pipeline halts and sends an infra alert if exceeded

**Non-goals:** EC2 deployment, CI/CD, author enrichment, Slack interactive buttons (CLI-only for handled).

### Session 5 — Deployment and observability

**Scope:** EC2 provisioning, systemd template unit, GitHub Actions CI/CD, SSM Parameter Store integration, structured logging, health endpoint.

**Acceptance:**
- `deploy/bootstrap-ec2.sh` provisions a fresh t4g.micro in us-east-1: user, directory structure, systemd units installed, IAM role attached for SSM read
- t4g.micro on 1-year Savings Plan (~$3.50/mo), 10 GB gp3 EBS, public subnet with no inbound SG rules, SSM Session Manager for access
- `deploy/social-surveyor@.service` runs the process under a dedicated `social-surveyor` user with `Restart=always`
- `.github/workflows/deploy.yml` on push to `main`: lints, tests, SSHes to EC2, runs `deploy/deploy.sh` (rsync code, uv sync, systemctl restart all enabled instances)
- Secrets in SSM at `/social-surveyor/<name>`, read via boto3, cached in-process
- structlog JSON output to stdout, captured by journald, optionally forwarded to CloudWatch
- `/health` endpoint on localhost:8080: last successful poll per source per project, DB row counts, last digest send, current daily cost
- Access `/health` from laptop via SSM port forwarding
- Infra alerts (poll failures, classification error rate >5%, cost cap hit) go to a separate Slack channel distinct from business alerts
- README has a "deploy your own" section that works end-to-end for a fresh forker

**Non-goals:** RSS, author enrichment, multi-region.

### Session 5.5 — Public/private config split (conditional)

**Trigger:** If by the end of session 5 the classifier prompt and ICP description reveal competitive positioning you'd rather not telegraph publicly, do this session. Otherwise skip.

**Scope:** Split sensitive config fields into `projects/<name>/private.yaml`, update config loader to merge, update `.gitignore`, document the pattern.

**Acceptance:**
- `projects/<name>/private.yaml` is in `.gitignore`
- `projects/<name>/private.example.yaml` committed, showing structure
- Config loader merges `classifier.yaml` + `private.yaml`, with private overriding public
- README documents the pattern for forkers who want to do the same
- Existing configs migrated without behavior change (eval set still passes)
- Evals labeled.jsonl optionally moves to gitignored location (user's call at this point)

**Non-goals:** Everything else.

### Session 6 — RSS and blog sources

**Scope:** RSS source module (Medium tags, Dev.to tags, Lobste.rs, individual Substacks).

**Acceptance:**
- `rss.yaml` schema: list of feed URLs with optional per-feed keyword filters
- `social-surveyor poll --project <name> --source rss` works
- Feed parser handles malformed XML gracefully (feedparser handles most of this)
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
- **No ORM.** Raw SQL against SQLite via `sqlite3` stdlib. The schema is 3 tables; SQLAlchemy would be overkill.
- **No web framework.** The `/health` endpoint is a stdlib `http.server`, nothing fancier.
- **No Docker.** systemd + virtualenv + uv on the EC2 host. Simpler, smaller, faster.

---

## Open questions

Updated as sessions reveal new decisions. Current open items:

- **X API authentication flow in pay-per-use:** still beta-gated as of April 2026; if we can't get pay-per-use access, session 2's X source falls back to legacy Basic tier ($200/mo) or gets deprioritized until we can. Verify before starting session 2.
- **Slack interactive buttons vs CLI-only handled:** the Slack button approach requires exposing an HTTPS endpoint to Slack, which complicates the EC2 SG setup. CLI-only (`social-surveyor handled <id>`) is the MVP choice. Revisit after session 5 if the CLI workflow feels clunky in daily use.
- **Rate limit for Anthropic API calls:** during backfill operations we could hit Anthropic's tier-based rate limits. Need concurrency control in `classifier.py` — single worker with configurable RPS cap is simplest.
- **Eval set size:** 50 items is the starting target; real accuracy tuning may need 200+. Budget time in session 3 for hand-labeling to keep pace.
- **Multi-project conflicts:** if two projects poll the same subreddit, we fetch and store twice. Acceptable for now (different projects have different classifiers, different storage). Revisit if it becomes a cost issue.

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
