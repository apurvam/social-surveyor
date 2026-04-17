# CLAUDE.md

Project conventions and working agreements for Claude Code sessions in this repo.

Read this file at the start of every session. Read PLAN.md when you need to understand the product scope, the session plan, or current acceptance criteria.

---

## Working style

- **One session, one PR.** Every Claude Code session corresponds to one session from PLAN.md and produces one pull request. Don't start a new session's work until the current one is merged.
- **Acceptance criteria are contracts.** Each session has acceptance criteria in PLAN.md. Those are what "done" means. Don't add features beyond them; don't skip them. If you think a criterion is wrong, raise it explicitly rather than silently ignoring it.
- **Update PLAN.md in the same PR when the plan evolves.** If you discover something during a session that changes future sessions — new open questions, scope adjustments, a decision that unblocks something — update PLAN.md as part of the session's PR. PLAN.md is a living document.
- **Ask before scope creep.** If something feels like it belongs to a later session, note it and defer. Don't pull session 5 work into session 2 because it seemed easy.

---

## Tooling

- **Python 3.12.** Use modern syntax. `from __future__ import annotations` at the top of every module. `|` union types, not `Union`. `list[int]`, not `List[int]`.
- **uv for package management.** Never run `pip` directly. Dependencies go in `pyproject.toml`; `uv add` to add them. `uv.lock` is committed. `uv sync` to install. `uv run <cmd>` to run commands in the venv.
- **ruff for lint and format.** `ruff check` and `ruff format` before every commit. Config in `pyproject.toml`. CI enforces this.
- **pytest for tests.** Unit tests mock all external APIs. Integration tests requiring live APIs are gated behind `pytest --live` and are skipped by default.
- **pydantic v2 for all config.** Every YAML file has a corresponding pydantic model. Loading config = parsing YAML + validating with the model. Invalid configs fail fast with readable errors.
- **structlog for logs, JSON output.** Every log event includes `project` (when applicable), `stage`, `source`, `item_id` (when applicable). No bare `print()` outside of the CLI's intentional user-facing output.
- **Typer for the CLI.** One entry point defined in `pyproject.toml` as `social-surveyor = "social_surveyor.cli:app"`. All subcommands require `--project <n>` unless the command is project-agnostic (e.g., `--version`).

---

## Code conventions

- **Type hints everywhere.** Including on private functions. `mypy --strict` should pass, though we don't enforce it in CI yet.
- **No ORM.** Use `sqlite3` from stdlib. The schema is small; SQL strings are fine. Parameterize everything (no string concatenation in queries).
- **No web framework for the health endpoint.** `http.server` from stdlib. It only serves one endpoint on localhost.
- **No unnecessary abstractions.** Don't introduce a `BaseSomethingFactoryProvider` pattern. The `Source` ABC is the only abstraction we need in the MVP because it lets us swap platforms cleanly. Everything else should be concrete functions.
- **One module, one responsibility.** `classifier.py` does classification. It doesn't know about Slack. `notifier.py` knows about Slack but not about LLMs. Don't cross streams.
- **Imports at the top.** No lazy imports inside functions unless there's a specific circular-import reason to do so.
- **Prefer pure functions.** Side effects (DB writes, API calls, logging) should be concentrated in a few obvious places. Business logic (routing decisions, prefilter matching) should be pure and trivial to test.

---

## Commits and PRs

- **Conventional commits.** Commit messages follow `<type>(<scope>): <subject>`. Types: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`. Example: `feat(sources): add reddit source with oauth`.
- **Small, single-purpose commits.** If a commit message needs "and" in it, split the commit.
- **PR description template.** Every PR description includes: which session it implements, a checklist of the session's acceptance criteria (checked as they're satisfied), and a "how to verify" section with commands the human can run to confirm it works.

---

## What to do when uncertain

- **When a session's scope is ambiguous,** re-read the session description in PLAN.md, then ask the user a specific question. Don't guess.
- **When a dependency choice isn't obvious,** prefer stdlib, then well-known packages (requests, httpx, pydantic, structlog, typer). If a new dependency seems needed, call it out in the PR description with a one-line justification.
- **When a test fails and you're not sure why,** don't delete or skip the test. Investigate. If the test is wrong, fix the test and explain why in the commit. If the code is wrong, fix the code.
- **When the user asks for something that seems to contradict PLAN.md,** surface the contradiction explicitly: "PLAN.md says X in session N; you're asking for Y; should I update PLAN.md or defer?"

---

## What not to do

- **Don't add secrets to the repo.** Ever. Not even in comments. Not even "temporarily." Secrets live in env vars (dev) or SSM Parameter Store (prod); configs reference them by name only.
- **Don't commit `data/*.db`, `.env`, `__pycache__/`, `.venv/`, `uv.lock` stuff that belongs ignored.** `.gitignore` handles most of this; respect it.
- **Don't introduce frameworks we don't need.** No FastAPI for a localhost health endpoint. No SQLAlchemy for three tables. No Celery for a single-process scheduler. No Docker for a systemd service.
- **Don't run destructive commands without asking.** `rm -rf`, `DROP TABLE`, `git push --force` — always confirm with the user first.
- **Don't make live API calls in CI.** Reddit, X, Anthropic — all mocked in tests by default. Live integration tests require the `--live` flag.
- **Don't pull in work from future sessions.** If session 2 "just needs a classifier too," it doesn't. Finish session 2 as scoped. Session 3 exists for a reason.

---

## Context about the user

Apurva (the user running this project) is a startup CEO with 15 years of infrastructure experience. Deep familiarity with Python, Go, SQL, distributed systems, AWS. Doesn't need hand-holding on engineering fundamentals; does appreciate calling out tradeoffs where they exist. Prefers concrete answers over "it depends." Values shipping working software over comprehensive designs.

The project is both a practical tool (monitoring for his startup opendata-timeseries) and an open-source artifact (MIT-licensed, intended for others to fork). Design decisions should serve both audiences.
