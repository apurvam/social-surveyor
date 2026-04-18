from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import structlog
import typer
from dotenv import load_dotenv

from . import __version__
from .config import ConfigError, ProjectConfig, load_project_config
from .log_config import configure_logging
from .sources.base import Source, SourceInitError
from .sources.github import GitHubSource
from .sources.hackernews import HackerNewsSource
from .sources.x import XSource
from .storage import Storage
from .types import RawItem

app = typer.Typer(
    name="social-surveyor",
    help="Self-hosted social listening pipeline.",
    no_args_is_help=True,
    add_completion=False,
)

log = structlog.get_logger(__name__)


# Sources this CLI knows how to build, in poll order. Each entry maps a
# source name to a (config-accessor, builder) pair. Builder signature is
# (project_config, storage) -> Source; not every source uses the storage,
# but passing it uniformly keeps the CLI simple.
SourceBuilder = Callable[[ProjectConfig, Storage], Source]
SOURCE_BUILDERS: dict[str, SourceBuilder] = {
    # "reddit" is temporarily absent between session 2.5 commits A1 and
    # A3: PRAW source has been preserved as reddit_api.py (dormant); the
    # RSS-based replacement lands in A3.
    "hackernews": lambda cfg, db: HackerNewsSource(cfg.hackernews, db),  # type: ignore[arg-type]
    "github": lambda cfg, db: GitHubSource(cfg.github, db),  # type: ignore[arg-type]
    "x": lambda cfg, db: XSource(cfg.x, db),  # type: ignore[arg-type]
}


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"social-surveyor {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = False,
    log_level: Annotated[
        str,
        typer.Option("--log-level", help="Log level (DEBUG, INFO, WARNING, ERROR)."),
    ] = "INFO",
) -> None:
    """social-surveyor — self-hosted social listening."""
    load_dotenv()
    configure_logging(log_level)


def _load_or_exit(project: str) -> ProjectConfig:
    try:
        return load_project_config(project)
    except ConfigError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=2) from None


def _configured_source_names(cfg: ProjectConfig) -> list[str]:
    names = []
    # "reddit" is not listed here between A1 and A3; it is re-added when
    # the RSS-based RedditSource is registered in SOURCE_BUILDERS.
    if cfg.hackernews is not None:
        names.append("hackernews")
    if cfg.github is not None:
        names.append("github")
    if cfg.x is not None:
        names.append("x")
    return names


def _select_source_names(cfg: ProjectConfig, source_filter: str | None) -> list[str]:
    configured = _configured_source_names(cfg)
    if source_filter is None:
        if not configured:
            raise typer.BadParameter(f"project '{cfg.name}' has no sources configured")
        return configured
    if source_filter not in SOURCE_BUILDERS:
        raise typer.BadParameter(
            f"unknown source '{source_filter}'; known: {', '.join(SOURCE_BUILDERS)}"
        )
    if source_filter not in configured:
        raise typer.BadParameter(
            f"source '{source_filter}' is not configured for project '{cfg.name}'"
        )
    return [source_filter]


def _build_source(name: str, cfg: ProjectConfig, storage: Storage) -> Source:
    try:
        return SOURCE_BUILDERS[name](cfg, storage)
    except SourceInitError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=2) from None


def _db_path(project: str) -> Path:
    return Path("data") / f"{project}.db"


def _item_to_dict(item: RawItem) -> dict[str, object]:
    d = asdict(item)
    d["created_at"] = item.created_at.isoformat()
    return d


def _print_json(data: object) -> None:
    typer.echo(json.dumps(data, default=str))


@app.command()
def poll(
    project: Annotated[str, typer.Option("--project", help="Project name.")],
    source: Annotated[
        str | None,
        typer.Option("--source", help="Limit poll to a single source. Omit to poll all."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help=(
                "Print items to stdout; don't write to DB. For the X source, "
                "does NOT hit the API — prints query set and prior state only."
            ),
        ),
    ] = False,
) -> None:
    """Poll configured sources for a project.

    With no --source, every configured source is polled sequentially. A
    failure in one source (e.g. GitHub rate limit) is logged but does
    not stop the remaining sources.
    """
    cfg = _load_or_exit(project)
    names = _select_source_names(cfg, source)

    if dry_run:
        _run_dry_run(cfg, names, project=project)
        return

    real_db = Storage(_db_path(project))
    try:
        for name in names:
            src = _build_source(name, cfg, real_db)
            try:
                items = src.fetch()
            except Exception as exc:
                log.exception(
                    "poll.source.failed",
                    project=project,
                    source=src.name,
                    error=repr(exc),
                )
                continue
            new = sum(1 for i in items if real_db.upsert_item(i))
            log.info(
                "poll.done",
                project=project,
                source=src.name,
                fetched=len(items),
                new=new,
            )
    finally:
        real_db.close()


def _run_dry_run(cfg: ProjectConfig, names: list[str], *, project: str) -> None:
    """Dry-run implementation.

    For the X source, calls the source's ``dry_run_state()`` against the
    real DB — never hitting the X API — and prints the config snapshot.
    For every other source, constructs it with a throwaway in-memory DB
    so fetch() doesn't mutate the real one, then streams items to stdout.
    """
    total = 0
    real_db_path = _db_path(project)
    real_db_exists = real_db_path.is_file()

    for name in names:
        if name == "x":
            # Need real DB to read prior cursors/usage — no writes happen.
            # If the project hasn't been polled yet, fall back to a fresh
            # in-memory DB so reads return empty cleanly.
            real_db = Storage(real_db_path) if real_db_exists else Storage(":memory:")
            try:
                x_source = _build_source(name, cfg, real_db)
                assert isinstance(x_source, XSource)
                _print_json({"source": name, "dry_run_state": x_source.dry_run_state()})
            finally:
                real_db.close()
            continue

        scratch_db = Storage(":memory:")
        try:
            src = _build_source(name, cfg, scratch_db)
            items = src.fetch()
            total += len(items)
            for item in items:
                _print_json(_item_to_dict(item))
        except Exception as exc:
            log.exception(
                "poll.dry_run.source.failed",
                project=project,
                source=name,
                error=repr(exc),
            )
        finally:
            scratch_db.close()
    log.info("poll.dry_run.done", project=project, fetched=total)


@app.command()
def backfill(
    project: Annotated[str, typer.Option("--project", help="Project name.")],
    source: Annotated[str, typer.Option("--source", help="Source to backfill.")],
    days: Annotated[int, typer.Option("--days", min=1, help="Days of history to fetch.")],
) -> None:
    """Fetch historical items for a single source.

    X backfill is served by Recent Search only (7-day cap). Full-archive
    search costs real money at a different tier and is a future session.
    """
    cfg = _load_or_exit(project)
    names = _select_source_names(cfg, source)
    (name,) = names

    real_db = Storage(_db_path(project))
    try:
        src = _build_source(name, cfg, real_db)
        items = src.backfill(days=days)
        new = sum(1 for i in items if real_db.upsert_item(i))
        log.info(
            "backfill.done",
            project=project,
            source=src.name,
            days=days,
            fetched=len(items),
            new=new,
        )
    finally:
        real_db.close()


@app.command()
def usage(
    project: Annotated[str, typer.Option("--project", help="Project name.")],
    source: Annotated[
        str | None,
        typer.Option("--source", help="Limit report to one source (defaults to all)."),
    ] = None,
) -> None:
    """Print today- and month-to-date API usage for cost-relevant sources.

    Reddit, Hacker News, and GitHub are free-tier and don't track usage.
    Only X tracks reads; its figures reflect the daily cap enforcement.
    """
    cfg = _load_or_exit(project)
    names = _select_source_names(cfg, source)

    db_path = _db_path(project)
    if not db_path.is_file():
        typer.echo(f"no DB at {db_path} yet — run a poll first", err=True)
        raise typer.Exit(code=1)

    now = datetime.now(UTC)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    db = Storage(db_path)
    try:
        report: dict[str, dict[str, object]] = {}
        for name in names:
            if name != "x":
                report[name] = {"tier": "free", "tracked": False}
                continue
            today_total = db.sum_api_usage("x", start_of_day)
            month_total = db.sum_api_usage("x", start_of_month)
            today_by_query = db.api_usage_by_query("x", start_of_day)
            report[name] = {
                "tier": "pay-per-use",
                "tracked": True,
                "used_today": today_total,
                "used_this_month": month_total,
                "daily_read_cap": cfg.x.daily_read_cap if cfg.x is not None else None,
                "today_by_query": today_by_query,
            }
        _print_json(report)
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)
