from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import structlog
import typer
from dotenv import load_dotenv

from . import __version__
from .config import ConfigError, ProjectConfig, load_project_config
from .logging import configure_logging
from .sources.base import Source
from .sources.reddit import RedditSource
from .storage import Storage
from .types import RawItem

app = typer.Typer(
    name="social-surveyor",
    help="Self-hosted social listening pipeline.",
    no_args_is_help=True,
    add_completion=False,
)

log = structlog.get_logger(__name__)


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


def _build_sources(cfg: ProjectConfig, source_filter: str | None) -> list[Source]:
    sources: list[Source] = []
    try:
        if cfg.reddit is not None and (source_filter is None or source_filter == "reddit"):
            sources.append(RedditSource(cfg.reddit))
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=2) from None

    if source_filter is not None and not sources:
        raise typer.BadParameter(
            f"source '{source_filter}' is not configured for project '{cfg.name}'"
        )
    if not sources:
        raise typer.BadParameter(f"project '{cfg.name}' has no sources configured")
    return sources


def _db_path(project: str) -> Path:
    return Path("data") / f"{project}.db"


def _print_item(item: RawItem) -> None:
    d = asdict(item)
    d["created_at"] = item.created_at.isoformat()
    typer.echo(json.dumps(d, default=str))


@app.command()
def poll(
    project: Annotated[str, typer.Option("--project", help="Project name.")],
    source: Annotated[
        str | None,
        typer.Option("--source", help="Limit poll to a single source."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print items to stdout; don't write to DB."),
    ] = False,
) -> None:
    """Poll configured sources for a project."""
    cfg = _load_or_exit(project)
    sources = _build_sources(cfg, source)

    if dry_run:
        total = 0
        for src in sources:
            items = src.fetch()
            total += len(items)
            for item in items:
                _print_item(item)
        log.info("poll.dry_run.done", project=project, fetched=total)
        return

    db = Storage(_db_path(project))
    try:
        for src in sources:
            items = src.fetch()
            new = sum(1 for i in items if db.upsert_item(i))
            log.info(
                "poll.done",
                project=project,
                source=src.name,
                fetched=len(items),
                new=new,
            )
    finally:
        db.close()


@app.command()
def backfill(
    project: Annotated[str, typer.Option("--project", help="Project name.")],
    source: Annotated[str, typer.Option("--source", help="Source to backfill.")],
    days: Annotated[int, typer.Option("--days", min=1, help="Days of history to fetch.")],
) -> None:
    """Fetch historical items for a source."""
    cfg = _load_or_exit(project)
    sources = _build_sources(cfg, source)
    (src,) = sources  # filter guarantees exactly one

    db = Storage(_db_path(project))
    try:
        items = src.backfill(days=days)
        new = sum(1 for i in items if db.upsert_item(i))
        log.info(
            "backfill.done",
            project=project,
            source=src.name,
            days=days,
            fetched=len(items),
            new=new,
        )
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)
