# social-surveyor

A self-hosted social listening pipeline for founders and small teams.
Polls Reddit (and later Hacker News, GitHub, X, RSS), dedupes, classifies
matching posts with an LLM tuned to your ICP, and routes results to Slack.

See [`PLAN.md`](./PLAN.md) for the full product plan and session roadmap,
and [`docs/getting-started.md`](./docs/getting-started.md) for setup.

## Status

Session 2: Reddit + Hacker News + GitHub (issues and matching comments)
+ X (Twitter) Recent Search with cost guardrails. All sources dedupe
by `(source, platform_id)` and write to one SQLite file per project.
No LLM, no Slack yet — those land in sessions 3 and 4.

## Quick start

```bash
brew install uv                               # or see https://docs.astral.sh/uv/
uv sync
cp .env.example .env                          # fill in creds for the sources you use
uv run social-surveyor poll --project opendata --dry-run
uv run social-surveyor poll --project opendata
uv run social-surveyor backfill --project opendata --source reddit --days 7
uv run social-surveyor usage --project opendata --source x
```

## Developer workflow

```bash
uv run ruff check
uv run ruff format
uv run pytest
```

## License

MIT — see [`LICENSE`](./LICENSE).
