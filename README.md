# social-surveyor

A self-hosted social listening pipeline for founders and small teams.
Polls Reddit, Hacker News, GitHub, and X for conversations that match
your ICP, classifies them with a Claude Haiku prompt you own, and
routes results to Slack — urgent items immediately, everything else in
a daily digest.

See [`PLAN.md`](./PLAN.md) for the full product plan and session
roadmap.

## Status

**Session 4 shipped:** Slack integration, urgency-based routing,
immediate alerts, daily digest, correction workflow (label / silence /
ingest), APScheduler-based `run` loop.

Session 5 wires this up on EC2 with systemd.

---

## Quickstart

From a fresh clone to receiving Slack alerts.

### 1. Install and sync

```bash
brew install uv                               # or https://docs.astral.sh/uv/
uv sync
```

### 2. Set up the project

```bash
uv run social-surveyor setup --project opendata
```

The wizard captures Reddit / GitHub / X / Anthropic credentials and
writes them to `.env`. GitHub and Reddit credentials are validated
against a free live call; X and Anthropic tokens are syntax-checked
only (live-validating every setup run would cost real money).

### 3. Create two Slack incoming webhooks

In Slack, go to **Apps → Manage → Custom Integrations → Incoming
Webhooks** (or the modern equivalent at `api.slack.com/apps`). Create
two webhooks:

- One pointing at your **immediate-alert** channel
- One pointing at your **daily-digest** channel

Add their URLs to `.env`:

```
OPENDATA_SLACK_WEBHOOK_IMMEDIATE=https://hooks.slack.com/services/...
OPENDATA_SLACK_WEBHOOK_DIGEST=https://hooks.slack.com/services/...
```

### 4. (Optional) Upload source emoji

The alerts reference four custom emoji: `:reddit:`, `:hn:`, `:x:`,
`:github:`. Upload matching PNGs under Slack's **Workspace Settings →
Customize → Emoji**. If you skip this, Slack renders the shortcode as
literal text — works, just less pretty.

### 5. Prime the pipeline

```bash
uv run social-surveyor poll --project opendata          # pull items
uv run social-surveyor classify --project opendata      # Haiku-label them
uv run social-surveyor route --project opendata         # fan out to Slack
uv run social-surveyor digest --project opendata --dry-run   # eyeball the digest shape
```

### 6. Run the long-lived loop

```bash
uv run social-surveyor run --project opendata
```

`run` schedules poll / classify / route every 10 minutes and posts the
daily digest at the time configured in `projects/opendata/routing.yaml`.
Ctrl-C stops it. Session 5 wraps this under systemd for production.

---

## Daily workflow

- **Morning:** check the digest in Slack. Urgent items should already
  have hit the immediate channel.
- **Something looks wrong?** The digest (and each alert) includes the
  item ID. Copy-paste the correction command:
  ```bash
  sv label --project opendata --item-id <id> --category <cat> --urgency <n>
  ```
  This appends a correction to `labeled.jsonl` without overwriting the
  original — latest-wins at eval time.
- **Noisy item you don't want re-alerting:**
  ```bash
  sv silence --project opendata --item-id <id>
  ```
  Silencing filters the router without teaching the classifier. Use
  this when the classifier's judgment was reasonable but the item
  isn't useful for your workflow.
- **Saw something great elsewhere?** Paste the URL:
  ```bash
  sv ingest --project opendata --url https://news.ycombinator.com/item?id=42
  ```
  Fetches + classifies + prints. No routing — you already saw it.
- **Weekly / monthly health check:**
  ```bash
  sv eval --project opendata --since 2026-04-01
  ```
  Narrows to items labeled since the date and shows how the active
  prompt is doing on them. Useful for drift detection.

### Recovering from mistakes

Silence is permanent by design (no `unsilence` command). To reverse a
silence:

```bash
sqlite3 data/opendata.db "DELETE FROM silenced_items WHERE item_id='<id>'"
```

Mis-classifications don't need special recovery — a new
`sv label --item-id` is an append, not an overwrite, so the latest
label wins automatically.

### The `sv` alias

Across this README, `sv` is shorthand for the full CLI entry point.
Add it to your shell rc file (`.zshrc` / `.bashrc`) once:

```bash
alias sv='uv run --project ~/workspace/social-surveyor social-surveyor'
```

For the EC2 box (Session 5):

```bash
alias sv='ssh-via-ssm social-surveyor -- social-surveyor'
```

…where `ssh-via-ssm` is whatever wrapper you use to invoke an SSM
Session Manager session. `aws ssm start-session --target i-...` works
if you prefer the raw command.

---

## Configuration

Each project lives under `projects/<name>/` with four YAML files:

| File              | Purpose                                                       |
| ----------------- | ------------------------------------------------------------- |
| `sources/*.yaml`  | Per-source queries / subreddits / GitHub orgs / X bearer cap  |
| `categories.yaml` | Category taxonomy + urgency scale (labeler + classifier)      |
| `classifier.yaml` | ICP description, few-shot examples, model, `prompt_version`   |
| `routing.yaml`    | Immediate-alert rules, digest schedule, cost caps             |

Secrets are referenced by env-var name only; `.env` holds the values.
See `projects/example/` for a fork-ready reference.

---

## Developer workflow

```bash
uv run ruff check
uv run ruff format
uv run pytest
```

Tests run entirely against mocked transports. Live-API integration
tests require `uv run pytest --live` and hit real services.

## License

MIT — see [`LICENSE`](./LICENSE).
