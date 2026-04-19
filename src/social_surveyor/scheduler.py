"""APScheduler wiring for the ``run`` long-running process.

Design: one :class:`BlockingScheduler` per project process. Four jobs:

- ``poll`` every ``poll_interval_minutes`` (default 10)
- ``classify`` every ``classify_interval_minutes`` (default 10)
- ``route`` every ``route_interval_minutes`` (default 10) — also posts
  pending immediate alerts in the same cycle
- ``digest`` daily at ``digest.schedule.hour:minute`` in the configured
  timezone

Jobs call the same ``run_*`` functions the CLI subcommands invoke, so
"what ``run`` does every 10 minutes" is exactly "what ``sv route``
does when you invoke it by hand." No separate codepath for scheduled
vs manual runs — reduces the surface where the two can drift.

Job-level exceptions are logged and swallowed; a single failing poll
cycle must not kill the process. APScheduler's executor emits its own
events too, but explicit try/except gives us clean structlog fields.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import structlog
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .config import RoutingConfig

log = structlog.get_logger(__name__)


def build_scheduler(
    project: str,
    *,
    routing_cfg: RoutingConfig,
    poll_fn: Callable[[], Any],
    classify_fn: Callable[[], Any],
    route_fn: Callable[[], Any],
    digest_fn: Callable[[], Any],
    poll_interval_minutes: int = 10,
    classify_interval_minutes: int = 10,
    route_interval_minutes: int = 10,
    scheduler: BlockingScheduler | None = None,
) -> BlockingScheduler:
    """Assemble the scheduler with all four jobs wired up.

    The four ``*_fn`` callables take no args — the caller closes over
    project, db_path, etc. Tests pass simple lambdas; production binds
    the real ``run_*`` helpers inside the ``run`` CLI command.
    """
    sched = scheduler or BlockingScheduler(timezone=routing_cfg.digest.schedule.timezone)

    sched.add_job(
        lambda: _safe_run("poll", poll_fn),
        IntervalTrigger(minutes=poll_interval_minutes),
        id=f"poll:{project}",
    )
    sched.add_job(
        lambda: _safe_run("classify", classify_fn),
        IntervalTrigger(minutes=classify_interval_minutes),
        id=f"classify:{project}",
    )
    sched.add_job(
        lambda: _safe_run("route", route_fn),
        IntervalTrigger(minutes=route_interval_minutes),
        id=f"route:{project}",
    )
    sched.add_job(
        lambda: _safe_run("digest", digest_fn),
        CronTrigger(
            hour=routing_cfg.digest.schedule.hour,
            minute=routing_cfg.digest.schedule.minute,
            timezone=routing_cfg.digest.schedule.timezone,
        ),
        id=f"digest:{project}",
    )
    return sched


def _safe_run(job_name: str, fn: Any) -> None:
    """Invoke ``fn``; log and swallow exceptions so the scheduler survives."""
    try:
        fn()
    except Exception as exc:
        log.exception("scheduler.job.failed", job=job_name, error=repr(exc))


__all__ = ["build_scheduler"]
