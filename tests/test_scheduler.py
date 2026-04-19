from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from social_surveyor.config import (
    DigestConfig,
    DigestScheduleConfig,
    ImmediateConfig,
    RoutingConfig,
)
from social_surveyor.scheduler import build_scheduler


def _cfg(tz: str = "UTC", hour: int = 9, minute: int = 0) -> RoutingConfig:
    return RoutingConfig(
        immediate=ImmediateConfig(
            threshold_urgency=7,
            alert_worthy_categories=["cost_complaint"],
            webhook_secret="X",
        ),
        digest=DigestConfig(
            schedule=DigestScheduleConfig(hour=hour, minute=minute, timezone=tz),
            webhook_secret="Y",
            window_hours=24,
        ),
    )


def test_scheduler_registers_four_jobs_with_correct_triggers() -> None:
    # Use BackgroundScheduler (a BlockingScheduler subclass-ish) so we
    # can inspect jobs without calling start(). Since our production
    # code uses BlockingScheduler, pass a Background one in via the
    # scheduler= param.
    calls: dict[str, int] = {"poll": 0, "classify": 0, "route": 0, "digest": 0}

    bg = BackgroundScheduler(timezone="UTC")
    sched = build_scheduler(
        "demo",
        routing_cfg=_cfg(hour=9, minute=30, tz="America/Los_Angeles"),
        poll_fn=lambda: calls.__setitem__("poll", calls["poll"] + 1),
        classify_fn=lambda: calls.__setitem__("classify", calls["classify"] + 1),
        route_fn=lambda: calls.__setitem__("route", calls["route"] + 1),
        digest_fn=lambda: calls.__setitem__("digest", calls["digest"] + 1),
        poll_interval_minutes=10,
        classify_interval_minutes=10,
        route_interval_minutes=10,
        scheduler=bg,
    )
    jobs = {j.id: j for j in sched.get_jobs()}
    assert set(jobs.keys()) == {
        "poll:demo",
        "classify:demo",
        "route:demo",
        "digest:demo",
    }

    assert isinstance(jobs["poll:demo"].trigger, IntervalTrigger)
    assert jobs["poll:demo"].trigger.interval.total_seconds() == 600
    assert isinstance(jobs["digest:demo"].trigger, CronTrigger)
    # Cron fields expose the scheduled hour/minute.
    fields = {f.name: str(f) for f in jobs["digest:demo"].trigger.fields}
    assert fields["hour"] == "9"
    assert fields["minute"] == "30"


def test_scheduler_job_exception_is_swallowed() -> None:
    """A failing job must not crash the scheduler. The safe-run wrapper
    catches and logs; subsequent jobs still fire."""
    bg = BackgroundScheduler(timezone="UTC")
    # Trip a callable and verify the wrapper doesn't propagate.
    sched = build_scheduler(
        "demo",
        routing_cfg=_cfg(),
        poll_fn=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        classify_fn=lambda: None,
        route_fn=lambda: None,
        digest_fn=lambda: None,
        scheduler=bg,
    )
    # The safe-run wrapper is the registered callable; calling it
    # directly should not raise.
    poll_job = next(j for j in sched.get_jobs() if j.id == "poll:demo")
    poll_job.func()  # no exception
