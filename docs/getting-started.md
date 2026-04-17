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

## Reddit OAuth

1. Go to https://www.reddit.com/prefs/apps and click **create another app**.
2. Pick **script**, give it any name, and set redirect URI to
   `http://localhost:8080` (unused in practice — we do application-only OAuth).
3. Note the 14-character client ID under the app name and the client secret.
4. Copy `.env.example` to `.env` and fill in:

```
REDDIT_CLIENT_ID=...
REDDIT_CLIENT_SECRET=...
REDDIT_USER_AGENT=social-surveyor/0.1 by <your-reddit-username>
```

`social-surveyor` loads `.env` automatically via python-dotenv.

## Verify the CLI

```bash
uv run social-surveyor --help
uv run social-surveyor --version
```

## Sanity-check with a dry run

`--dry-run` fetches from Reddit and prints items to stdout without
touching the database:

```bash
uv run social-surveyor poll --project opendata --source reddit --dry-run
```

Each line is a JSON blob representing one `RawItem`. If this works,
your Reddit creds are good.

## Start collecting data

Drop `--dry-run` to persist to `data/opendata.db`:

```bash
uv run social-surveyor poll --project opendata --source reddit
```

Re-running is safe — dedupe is scoped by `(source, platform_id)`, so
duplicates are silently skipped.

## Backfill recent history

```bash
uv run social-surveyor backfill --project opendata --source reddit --days 7
```

Reddit's search API has coarse time filters (day/week/month/year/all);
we pick the narrowest bucket that covers the window and re-filter
client-side to the exact cutoff.

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
