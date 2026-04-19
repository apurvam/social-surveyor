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
from .cli_classify import run_classify
from .cli_digest import run_digest
from .cli_eval import (
    HAIKU_INPUT_USD_PER_MTOK,
    HAIKU_OUTPUT_USD_PER_MTOK,
    run_eval,
)
from .cli_explain import run_explain
from .cli_ingest import run_ingest
from .cli_label import run_label, run_label_item
from .cli_route import run_route
from .cli_setup import run_setup
from .cli_silence import run_silence
from .cli_stats import run_stats
from .cli_triage import run_triage
from .config import ConfigError, ProjectConfig, load_project_config, load_routing_config
from .log_config import configure_logging
from .sources.base import Source, SourceInitError
from .sources.github import GitHubSource
from .sources.hackernews import HackerNewsSource
from .sources.reddit import RedditSource
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
    "reddit": lambda cfg, _: RedditSource(cfg.reddit),  # type: ignore[arg-type]
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
    if cfg.reddit is not None:
        names.append("reddit")
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


def run_poll(
    *,
    project: str,
    projects_root: Path = Path("projects"),
    source: str | None = None,
) -> None:
    """Poll configured sources for ``project``.

    Extracted from the ``poll`` CLI command so the scheduler can call
    the same function without going through typer. A failure in one
    source (e.g. GitHub rate limit) is logged but doesn't stop the
    remaining sources.
    """
    try:
        cfg = load_project_config(project, projects_root=projects_root)
    except ConfigError as e:
        raise typer.BadParameter(str(e)) from None
    names = _select_source_names(cfg, source)

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

    run_poll(project=project, source=source)


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
        typer.Option(
            "--source",
            help=(
                "Limit report to one source. 'anthropic' reports Haiku "
                "classification token spend; all other names map to poll "
                "sources."
            ),
        ),
    ] = None,
) -> None:
    """Print today- and month-to-date API usage for cost-relevant sources.

    Reddit, Hacker News, and GitHub are free-tier and don't track
    usage. X tracks reads; anthropic tracks classification tokens.
    """
    db_path = _db_path(project)
    if not db_path.is_file():
        typer.echo(f"no DB at {db_path} yet — run a poll first", err=True)
        raise typer.Exit(code=1)

    now = datetime.now(UTC)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # anthropic is a reporting-only source — not a poll source — so it
    # doesn't need ProjectConfig validation.
    if source == "anthropic":
        db = Storage(db_path)
        try:
            report = _anthropic_usage_report(db, start_of_day, start_of_month)
            _print_json(report)
        finally:
            db.close()
        return

    cfg = _load_or_exit(project)
    names = _select_source_names(cfg, source)

    db = Storage(db_path)
    try:
        report_all: dict[str, dict[str, object]] = {}
        for name in names:
            if name != "x":
                report_all[name] = {"tier": "free", "tracked": False}
                continue
            today_total = db.sum_api_usage("x", start_of_day)
            month_total = db.sum_api_usage("x", start_of_month)
            today_by_query = db.api_usage_by_query("x", start_of_day)
            report_all[name] = {
                "tier": "pay-per-use",
                "tracked": True,
                "used_today": today_total,
                "used_this_month": month_total,
                "daily_read_cap": cfg.x.daily_read_cap if cfg.x is not None else None,
                "today_by_query": today_by_query,
            }
        _print_json(report_all)
    finally:
        db.close()


def _anthropic_usage_report(
    db: Storage,
    start_of_day: datetime,
    start_of_month: datetime,
) -> dict[str, object]:
    today_in, today_out = db.sum_api_tokens("anthropic", start_of_day)
    month_in, month_out = db.sum_api_tokens("anthropic", start_of_month)
    today_calls = db.sum_api_usage("anthropic", start_of_day)
    month_calls = db.sum_api_usage("anthropic", start_of_month)
    today_by_version = db.api_usage_by_query("anthropic", start_of_day)
    return {
        "source": "anthropic",
        "tier": "usage-based",
        "tracked": True,
        "today": {
            "calls": today_calls,
            "input_tokens": today_in,
            "output_tokens": today_out,
            "usd_estimate": _haiku_usd(today_in, today_out),
        },
        "month_to_date": {
            "calls": month_calls,
            "input_tokens": month_in,
            "output_tokens": month_out,
            "usd_estimate": _haiku_usd(month_in, month_out),
        },
        "today_by_prompt_version": today_by_version,
        "prices": {
            "input_usd_per_mtok": HAIKU_INPUT_USD_PER_MTOK,
            "output_usd_per_mtok": HAIKU_OUTPUT_USD_PER_MTOK,
        },
    }


def _haiku_usd(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens / 1_000_000 * HAIKU_INPUT_USD_PER_MTOK
        + output_tokens / 1_000_000 * HAIKU_OUTPUT_USD_PER_MTOK
    )


@app.command()
def label(
    project: Annotated[str, typer.Option("--project", help="Project name.")],
    source: Annotated[
        str | None,
        typer.Option("--source", help="Limit labeling to one source."),
    ] = None,
    resume: Annotated[
        bool,
        typer.Option(
            "--resume/--no-resume",
            help="Skip items already in labeled.jsonl. On by default.",
        ),
    ] = True,
    randomize: Annotated[
        bool,
        typer.Option("--random", help="Sample across the time range instead of newest-first."),
    ] = False,
    disagreements: Annotated[
        bool,
        typer.Option(
            "--disagreements",
            help=(
                "Walk labeled items whose classification disagrees with the "
                "human label, under the prompt_version from classifier.yaml "
                "(or --prompt-version). Useful after running eval to "
                "re-examine specific items."
            ),
        ),
    ] = False,
    prompt_version: Annotated[
        str | None,
        typer.Option(
            "--prompt-version",
            help="Override prompt_version for --disagreements mode.",
        ),
    ] = None,
    reconsider: Annotated[
        bool,
        typer.Option(
            "--reconsider",
            help=(
                "Walk already-labeled items to re-examine them against the "
                "current taxonomy. Use after extending categories.yaml to "
                "propagate the sharper definitions into labeled.jsonl. "
                "Enter keeps the current label; type a category to relabel."
            ),
        ),
    ] = False,
    category: Annotated[
        str | None,
        typer.Option(
            "--category",
            help=(
                "With --item-id, set the label category non-interactively. "
                "With --reconsider, filter the queue to items currently "
                "labeled as this category. Invalid without either."
            ),
        ),
    ] = None,
    urgency_min: Annotated[
        int | None,
        typer.Option(
            "--urgency-min",
            min=0,
            max=10,
            help="Filter --reconsider queue to items whose current urgency is >= this.",
        ),
    ] = None,
    urgency_max: Annotated[
        int | None,
        typer.Option(
            "--urgency-max",
            min=0,
            max=10,
            help="Filter --reconsider queue to items whose current urgency is <= this.",
        ),
    ] = None,
    item_id: Annotated[
        str | None,
        typer.Option(
            "--item-id",
            help=(
                "Label one specific item by canonical id ({source}:{platform_id}). "
                "Interactive unless --category and --urgency are also given."
            ),
        ),
    ] = None,
    label_urgency: Annotated[
        int | None,
        typer.Option(
            "--urgency",
            min=0,
            max=10,
            help="With --item-id, set the urgency (0-10) non-interactively.",
        ),
    ] = None,
    label_note: Annotated[
        str | None,
        typer.Option(
            "--note",
            help="With --item-id, attach an optional note.",
        ),
    ] = None,
) -> None:
    """Walk through unlabeled items and record category + urgency + optional note.

    Labels append to projects/<project>/evals/labeled.jsonl per decision
    so Ctrl-C loses at most one label. Use --item-id to label a specific
    item (e.g. from a Slack alert's copy-paste line); a relabel appends
    a new entry rather than overwriting, and the latest entry wins.
    """
    # --urgency / --note only make sense alongside --item-id. --category
    # is overloaded: it's either the target label (with --item-id) or
    # the reconsider-queue filter (with --reconsider). Reject ambiguous
    # combinations up-front rather than letting the wrong mode eat it.
    if item_id is None:
        if label_urgency is not None:
            raise typer.BadParameter("--urgency requires --item-id")
        if label_note is not None:
            raise typer.BadParameter("--note requires --item-id")
        if category is not None and not reconsider:
            raise typer.BadParameter(
                "--category requires --item-id (target label) or --reconsider (queue filter)"
            )
    else:
        if disagreements or reconsider:
            raise typer.BadParameter(
                "--item-id is mutually exclusive with --disagreements and --reconsider"
            )

    _load_or_exit(project)

    if item_id is not None:
        try:
            run_label_item(
                project,
                _db_path(project),
                Path("projects"),
                item_id=item_id,
                category=category,
                urgency=label_urgency,
                note=label_note,
            )
        except typer.BadParameter as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(code=1) from None
        return
    if not resume:
        # Legacy API kept so tests can exercise both paths; the queue
        # builder still uses the labeled-ids set, so --no-resume is
        # effectively identical to --resume today. Left as a knob for
        # future "start fresh" semantics (e.g., relabel from scratch).
        typer.echo(
            "(--no-resume is currently equivalent to --resume; items in labeled.jsonl are always skipped)"
        )

    disagreements_for_version: str | None = None
    if disagreements:
        # Resolve the effective prompt_version for disagreement mode:
        # CLI flag > classifier.yaml. Fail loudly if neither is usable
        # rather than silently defaulting.
        from .config import load_classifier_config

        try:
            clf_cfg = load_classifier_config(project)
        except ConfigError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(code=2) from None
        disagreements_for_version = prompt_version or clf_cfg.prompt_version

    try:
        result = run_label(
            project,
            _db_path(project),
            Path("projects"),
            source=source,
            randomize=randomize,
            disagreements_for_version=disagreements_for_version,
            reconsider=reconsider,
            reconsider_category=category,
            reconsider_urgency_min=urgency_min,
            reconsider_urgency_max=urgency_max,
        )
    except typer.BadParameter as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from None
    if reconsider:
        typer.echo(
            f"\ndone — relabeled={result['labeled']} kept={result.get('kept', 0)} "
            f"skipped={result['skipped']} remaining={result['remaining']}"
        )
    else:
        typer.echo(
            f"\ndone — labeled={result['labeled']} skipped={result['skipped']} "
            f"remaining={result['remaining']}"
        )


@app.command()
def silence(
    project: Annotated[str, typer.Option("--project", help="Project name.")],
    item_id: Annotated[
        str,
        typer.Option(
            "--item-id",
            help="Canonical id ({source}:{platform_id}) of the item to silence.",
        ),
    ],
) -> None:
    """Stop alerting on a specific item.

    Silence is non-teaching: it filters the router but does not update
    labeled.jsonl. Use `label --item-id` instead when the classifier's
    judgment was wrong.

    Silencing is permanent. To reverse a silence, run:

        sqlite3 data/<project>.db "DELETE FROM silenced_items WHERE item_id='<id>'"
    """
    _load_or_exit(project)
    try:
        run_silence(project, _db_path(project), item_id=item_id)
    except typer.BadParameter as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from None


@app.command()
def classify(
    project: Annotated[str, typer.Option("--project", help="Project name.")],
    item_id: Annotated[
        str | None,
        typer.Option(
            "--item-id",
            help="Classify exactly one item (canonical {source}:{platform_id}).",
        ),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", min=1, help="Cap on items classified in batch mode."),
    ] = None,
    prompt_version: Annotated[
        str | None,
        typer.Option(
            "--prompt-version",
            help="Override prompt_version from classifier.yaml for this run.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Build the prompt and print it; don't call the Anthropic API.",
        ),
    ] = False,
) -> None:
    """Classify unclassified items under the active prompt_version.

    Sequential, one item at a time — throughput optimization is a
    Session 5 concern. Re-running classify is safe: items that already
    have a classification for the active prompt_version are skipped
    (use --prompt-version v2 to re-classify them under a different
    version).
    """
    try:
        run_classify(
            project,
            _db_path(project),
            Path("projects"),
            item_id=item_id,
            limit=limit,
            prompt_version_override=prompt_version,
            dry_run=dry_run,
        )
    except typer.BadParameter as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from None


@app.command()
def eval(
    project: Annotated[str, typer.Option("--project", help="Project name.")],
    prompt_version: Annotated[
        str | None,
        typer.Option(
            "--prompt-version",
            help="Override the version to eval. Defaults to classifier.yaml.",
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="Print a per-item disagreement diff."),
    ] = False,
    export: Annotated[
        Path | None,
        typer.Option(
            "--export",
            help="Write the full eval run (metrics + disagreements) as JSON.",
        ),
    ] = None,
    re_score: Annotated[
        bool,
        typer.Option(
            "--re-score",
            help=(
                "Score existing cached classifications against the current "
                "labels — no API calls. Use after a taxonomy extension or "
                "relabel pass to see the delta without paying classification "
                "costs."
            ),
        ),
    ] = False,
) -> None:
    """Score the classifier against labeled.jsonl.

    Version-exact-match semantics: only classifications under the
    specified (or configured) prompt_version count as cache hits.
    Missing classifications get classified now — warm cache means <30s
    eval; cold start hits ~2s per Haiku call. ``--re-score`` skips the
    classifier entirely and reports missing-cache items as excluded.
    """
    try:
        run_eval(
            project,
            _db_path(project),
            Path("projects"),
            prompt_version_override=prompt_version,
            verbose=verbose,
            export_path=export,
            re_score=re_score,
        )
    except typer.BadParameter as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from None


@app.command()
def run(
    project: Annotated[str, typer.Option("--project", help="Project name.")],
    poll_interval_minutes: Annotated[
        int,
        typer.Option("--poll-interval-minutes", min=1, help="How often to poll sources."),
    ] = 10,
    classify_interval_minutes: Annotated[
        int,
        typer.Option(
            "--classify-interval-minutes",
            min=1,
            help="How often to classify unclassified items.",
        ),
    ] = 10,
    route_interval_minutes: Annotated[
        int,
        typer.Option(
            "--route-interval-minutes",
            min=1,
            help="How often to route classifications and post immediate alerts.",
        ),
    ] = 10,
) -> None:
    """Start the long-running pipeline: poll / classify / route / digest.

    Blocking foreground process. In production, systemd wraps this
    (Session 5). Locally, Ctrl-C stops it cleanly.
    """
    from .scheduler import build_scheduler

    _load_or_exit(project)
    try:
        routing_cfg = load_routing_config(project)
    except ConfigError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=2) from None

    db_path = _db_path(project)
    projects_root = Path("projects")

    # Bind context into zero-arg callables so the scheduler stays a
    # thin wiring layer.
    def _poll_job() -> None:
        run_poll(project=project, projects_root=projects_root)

    def _classify_job() -> None:
        run_classify(
            project,
            db_path,
            projects_root,
            item_id=None,
            limit=None,
            prompt_version_override=None,
            dry_run=False,
        )

    def _route_job() -> None:
        run_route(project, db_path, projects_root, dry_run=False)

    def _digest_job() -> None:
        run_digest(project, db_path, projects_root, dry_run=False)

    sched = build_scheduler(
        project,
        routing_cfg=routing_cfg,
        poll_fn=_poll_job,
        classify_fn=_classify_job,
        route_fn=_route_job,
        digest_fn=_digest_job,
        poll_interval_minutes=poll_interval_minutes,
        classify_interval_minutes=classify_interval_minutes,
        route_interval_minutes=route_interval_minutes,
    )

    typer.echo(
        f"social-surveyor running for project {project!r} "
        f"(poll/classify/route every {poll_interval_minutes}m, "
        f"digest at {routing_cfg.digest.schedule.hour:02d}:"
        f"{routing_cfg.digest.schedule.minute:02d} "
        f"{routing_cfg.digest.schedule.timezone}). Ctrl-C to stop."
    )
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        typer.echo("\nstopped.")


@app.command()
def route(
    project: Annotated[str, typer.Option("--project", help="Project name.")],
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print decisions; don't write alerts rows or POST to Slack.",
        ),
    ] = False,
) -> None:
    """Route unrouted classifications and send pending immediate alerts.

    Idempotent: classifications with an existing alerts row are skipped.
    Failed sends leave sent_at=NULL so the next invocation retries.
    """
    _load_or_exit(project)
    try:
        run_route(
            project,
            _db_path(project),
            Path("projects"),
            dry_run=dry_run,
        )
    except typer.BadParameter as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from None


@app.command()
def digest(
    project: Annotated[str, typer.Option("--project", help="Project name.")],
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print the Block Kit JSON to stdout; don't POST to Slack.",
        ),
    ] = False,
    category: Annotated[
        str | None,
        typer.Option(
            "--category",
            help=(
                "Skip Slack; print a full listing of this category to stdout. "
                "Respects --since and --limit. Pointed at by the main "
                "digest's overflow hint line."
            ),
        ),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help=(
                "ISO-8601 date (YYYY-MM-DD). Overrides the configured "
                "window_hours — useful for retrospective digests."
            ),
        ),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", min=1, help="Cap items when using --category."),
    ] = None,
) -> None:
    """Build and send the daily Slack digest (or inspect a category to stdout)."""
    _load_or_exit(project)

    since_dt: datetime | None = None
    if since is not None:
        try:
            since_dt = datetime.fromisoformat(since).replace(tzinfo=UTC)
        except ValueError:
            typer.echo(f"--since must be ISO-8601 (YYYY-MM-DD), got {since!r}", err=True)
            raise typer.Exit(code=1) from None

    try:
        run_digest(
            project,
            _db_path(project),
            Path("projects"),
            dry_run=dry_run,
            category=category,
            since=since_dt,
            limit=limit,
        )
    except typer.BadParameter as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from None


@app.command()
def ingest(
    project: Annotated[str, typer.Option("--project", help="Project name.")],
    url: Annotated[
        str,
        typer.Option("--url", help="URL of an HN / Reddit / X item to capture."),
    ],
) -> None:
    """Capture a manually-supplied URL: fetch, insert, classify.

    No Slack routing — you already saw the item; no need to re-surface.
    X ingestion counts against the daily read cap.
    """
    _load_or_exit(project)
    try:
        run_ingest(project, _db_path(project), Path("projects"), url=url)
    except typer.BadParameter as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from None


@app.command()
def explain(
    project: Annotated[str, typer.Option("--project", help="Project name.")],
    item_id: Annotated[
        str,
        typer.Option(
            "--item-id",
            help="Canonical id — {source}:{platform_id}, e.g. 'hackernews:41234567'.",
        ),
    ],
) -> None:
    """Dump raw item, effective label, every classification, and the current prompt."""
    try:
        run_explain(
            project,
            _db_path(project),
            Path("projects"),
            item_id=item_id,
        )
    except typer.BadParameter as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from None


@app.command()
def setup(
    project: Annotated[str, typer.Option("--project", help="Project name.")],
) -> None:
    """Interactive wizard: capture credentials, write .env, set reddit_username.

    No paid API calls. GitHub + Reddit validations are free (one
    request each); X and Anthropic are syntactic-only because every
    setup run making a paid call is the wrong pattern to build in.
    """
    try:
        run_setup(project, Path("projects"), Path(".env"))
    except typer.BadParameter as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=2) from None


@app.command()
def triage(
    project: Annotated[str, typer.Option("--project", help="Project name.")],
    source: Annotated[
        str | None,
        typer.Option("--source", help="Limit triage to one source."),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, help="Sample size per group."),
    ] = 8,
    window_days: Annotated[
        int,
        typer.Option("--window-days", min=1, help="How far back to pull samples."),
    ] = 30,
    preview_chars: Annotated[
        int,
        typer.Option(
            "--preview-chars",
            min=0,
            help=(
                "Body-preview length per item. Default 300. During the loop, "
                "type an item index (1..N) to expand that one to full body."
            ),
        ),
    ] = 300,
) -> None:
    """Walk query groups newest-first and record keep/drop/refine decisions.

    Writes a Markdown report to projects/<project>/triage_YYYYMMDD_HHMM.md
    with YAML-diff suggestions. The tool does NOT auto-edit source
    configs — you review the report and apply changes manually.
    """
    _load_or_exit(project)
    try:
        report_path = run_triage(
            project,
            _db_path(project),
            Path("projects"),
            source_filter=source,
            limit=limit,
            window_days=window_days,
            preview_chars=preview_chars,
        )
    except typer.BadParameter as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"\nwrote triage report: {report_path}")


@app.command()
def stats(
    project: Annotated[str, typer.Option("--project", help="Project name.")],
) -> None:
    """One-screen DB summary: per-source counts, query groups, label status.

    The "(unknown query)" bucket is pre-group_key items from before
    Session 2.75 — they age out as new polls come in; we deliberately
    don't backfill-by-inference.
    """
    # Loading the project config is optional for stats (we only need the
    # DB path), but it validates the project dir exists, giving a nicer
    # error than "no DB at …".
    _load_or_exit(project)
    db_path = _db_path(project)
    try:
        out = run_stats(project, db_path, Path("projects"))
    except typer.BadParameter as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(out)


if __name__ == "__main__":  # pragma: no cover
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)
