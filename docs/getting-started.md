# Getting started

This walks you through installing dependencies, configuring Reddit OAuth,
and running your first poll.

## Prerequisites

- Python 3.12 (uv can install it for you if it's missing)
- [uv](https://docs.astral.sh/uv/) for dependency management:
  `brew install uv` on macOS, or see the uv docs for Linux.

## Install

```bash
uv sync
```

This creates `.venv/`, installs pinned dependencies, and makes the
`social-surveyor` entry point available via `uv run`.

## Credentials

Copy `.env.example` to `.env` and fill in whichever sources you plan to
use. `social-surveyor` loads `.env` automatically via python-dotenv.

### Reddit (free)

1. Go to https://www.reddit.com/prefs/apps and click **create another app**.
2. Pick **script**, give it any name, set redirect URI to
   `http://localhost:8080` (unused — we do application-only OAuth).
3. Fill in the client id, secret, and user agent in `.env`.

### Hacker News (no auth)

Nothing to set. The Algolia search endpoint is public.

### GitHub (free tier, 5000 req/hour)

1. Create a token at https://github.com/settings/tokens. No scopes are
   required for public search.
2. Set `GITHUB_TOKEN` in `.env`.

### X (Twitter) — **paid**, read carefully

X Recent Search is pay-per-use at `$0.005/post read`. Each source has a
`daily_read_cap` in its YAML; polls that would exceed the cap are
skipped with a warning. Generate a bearer token at
https://developer.x.com/en/portal/dashboard and set `X_BEARER_TOKEN`.

The `--dry-run` flag on `poll` **does not call the X API** — it prints
the configured queries and prior cursors/usage instead. Use it
liberally.

## Verify the CLI

```bash
uv run social-surveyor --help
uv run social-surveyor --version
```

## Sanity-check with a dry run

`--dry-run` prints to stdout without touching the database. For X it
also does not hit the API — it prints your configured queries and any
prior cursor/usage state.

```bash
# Per-source dry runs
uv run social-surveyor poll --project opendata --source reddit --dry-run
uv run social-surveyor poll --project opendata --source hackernews --dry-run
uv run social-surveyor poll --project opendata --source github --dry-run
uv run social-surveyor poll --project opendata --source x --dry-run
```

## Poll all sources

```bash
uv run social-surveyor poll --project opendata
```

A failure in one source (e.g. GitHub rate limit) is logged and the
poll moves on to the next source.

## Backfill recent history

```bash
uv run social-surveyor backfill --project opendata --source reddit --days 7
uv run social-surveyor backfill --project opendata --source hackernews --days 7
uv run social-surveyor backfill --project opendata --source github --days 7
# X backfill is served by Recent Search only and caps at 7 days.
uv run social-surveyor backfill --project opendata --source x --days 7
```

## Check X usage

```bash
uv run social-surveyor usage --project opendata --source x
```

Prints today- and month-to-date post read counts and the configured
`daily_read_cap`.

## Inspect the database

```bash
sqlite3 data/opendata.db 'SELECT COUNT(*) FROM items'
sqlite3 data/opendata.db \
  "SELECT source, title, url FROM items ORDER BY created_at DESC LIMIT 10"
```

## Use your own project

Configs live under `projects/<name>/sources/`. Copy `projects/example/`
as a starting point, edit the subreddits and queries, then run the same
commands with `--project <your-name>`.
