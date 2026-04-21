"""Daily cost-cap enforcement for Anthropic (Haiku tokens) and X (post reads).

Both caps follow the same shape:

- A ``check_*_cap`` function classifies today's usage against a cap
  into ``ok`` / ``warn`` / ``halt``.
- An ``enforce_*_cap`` function runs the check, emits a ``warn`` log
  when ≥80% of cap, and on ``halt`` posts a ``fatal`` infra alert —
  idempotent per UTC day via the ``infra_alerts`` table so a 10-minute
  scheduler loop doesn't spam the channel.
- The caller takes the return value as a go/no-go: ``True`` continue,
  ``False`` skip the expensive work.

Haiku runs at the start of every classify invocation. X runs at the
start of every poll cycle, before any X search is issued. Day
rollover is UTC midnight for both.

Failure modes the caps guard against:

1. A runaway classification loop (stuck retry, reclassify-everything
   script left running) that chews through Anthropic credits.
2. An accidental backfill of a huge item set at full prompt cost.
3. A runaway X polling loop (e.g., ``since_id`` returning the same
   tweet repeatedly) that burns through X's per-post billing before a
   human notices.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import structlog

from .config import RoutingConfig
from .notifier import InfraAlertChannel, post_infra_alert
from .secrets import SecretNotFoundError, resolve_secret
from .storage import Storage

log = structlog.get_logger("cost_caps")

CapState = Literal["ok", "warn", "halt"]

WARN_FRACTION = 0.8

# Alert kinds used to de-dupe cost-cap infra alerts against the
# ``infra_alerts`` table. One row per (alert_kind, UTC date) means at
# most one cap-exceeded Slack post per day per cap.
HAIKU_CAP_ALERT_KIND = "haiku_cap_exceeded"
X_CAP_ALERT_KIND = "x_cap_exceeded"


@dataclass(frozen=True)
class HaikuCapCheck:
    """Result of a single cap check. Callers dispatch on ``state``."""

    state: CapState
    today_tokens: int
    cap: int

    @property
    def percent(self) -> float:
        return (self.today_tokens / self.cap * 100.0) if self.cap > 0 else 0.0


def today_haiku_tokens(db: Storage, *, now: datetime | None = None) -> int:
    """Sum of input+output tokens for the ``anthropic`` source since UTC midnight.

    Rows with NULL token columns (non-LLM sources) contribute 0 via the
    underlying ``sum_api_tokens`` helper, so this is safe to call against
    a fresh DB or one that predates Session 3's token tracking.
    """
    effective_now = (now or datetime.now(UTC)).astimezone(UTC)
    start_of_day = effective_now.replace(hour=0, minute=0, second=0, microsecond=0)
    in_tok, out_tok = db.sum_api_tokens("anthropic", start_of_day)
    return int(in_tok) + int(out_tok)


def check_haiku_cap(
    db: Storage,
    cap: int,
    *,
    now: datetime | None = None,
) -> HaikuCapCheck:
    """Classify today's token total against ``cap`` — ``ok``/``warn``/``halt``.

    A cap of 0 is treated as "unlimited" (no warn, no halt). Useful for
    tests that want to exercise the non-halted path without computing a
    realistic token budget.
    """
    total = today_haiku_tokens(db, now=now)
    if cap <= 0:
        state: CapState = "ok"
    elif total >= cap:
        state = "halt"
    elif total >= int(cap * WARN_FRACTION):
        state = "warn"
    else:
        state = "ok"
    return HaikuCapCheck(state=state, today_tokens=total, cap=cap)


def today_utc_iso(now: datetime | None = None) -> str:
    """ISO date string for today in UTC — idempotency key for infra alerts."""
    return (now or datetime.now(UTC)).astimezone(UTC).date().isoformat()


def resolve_infra_channel(routing_cfg: RoutingConfig) -> InfraAlertChannel | None:
    """Return the webhook target for infra alerts, or ``None`` if neither
    the dedicated infra webhook nor the immediate-channel fallback
    resolves.

    Resolution order:

    1. ``routing.infra.webhook_secret`` — dedicated infra channel, no
       prefix needed on the message.
    2. ``routing.immediate.webhook_secret`` — shared business channel,
       prepend ``[INFRA]`` so operators can still spot ops events in a
       feed otherwise dominated by content alerts.

    Both paths call :func:`secrets.resolve_secret` and therefore pick up
    the SSM fallback (Session 5a-polish Phase 3) without code change.
    """
    if routing_cfg.infra.webhook_secret is not None:
        try:
            url = resolve_secret(routing_cfg.infra.webhook_secret)
            return InfraAlertChannel(webhook_url=url, source="infra", prefix="")
        except SecretNotFoundError:
            log.warning(
                "cost_caps.infra_webhook_unresolved",
                secret=routing_cfg.infra.webhook_secret,
                fallback="immediate-with-prefix",
            )
    try:
        url = resolve_secret(routing_cfg.immediate.webhook_secret)
    except SecretNotFoundError:
        log.warning(
            "cost_caps.no_infra_channel_available",
            infra_secret=routing_cfg.infra.webhook_secret,
            immediate_secret=routing_cfg.immediate.webhook_secret,
        )
        return None
    return InfraAlertChannel(webhook_url=url, source="immediate-fallback", prefix="[INFRA] ")


def enforce_haiku_cap(
    db: Storage,
    routing_cfg: RoutingConfig,
    *,
    now: datetime | None = None,
    infra_channel: InfraAlertChannel | None = None,
    http_client: Any | None = None,
    echo_fn: Any = None,
) -> bool:
    """Run the cap check and post-once infra alert. Return True to
    continue classification, False to halt.

    The infra Slack post is bounded to once per UTC day via the
    ``infra_alerts`` table. Failures during the post are logged but
    don't raise — a transient Slack outage shouldn't block
    classification logic from short-circuiting.

    ``echo_fn`` (optional) echoes the halt reason to stdout so CLI
    operators see the cause without digging through journald. The
    scheduler uses ``None`` and relies on structlog.

    ``infra_channel`` is injectable for tests. When ``None``, resolves
    from ``routing_cfg`` on halt only (skipping the resolver when we
    never intend to post).
    """
    result = check_haiku_cap(db, routing_cfg.cost_caps.daily_haiku_tokens, now=now)

    if result.state == "warn":
        log.warning(
            "classifier.haiku_cap_approaching",
            today_tokens=result.today_tokens,
            cap=result.cap,
            percent=round(result.percent, 1),
        )
        return True

    if result.state != "halt":
        return True

    log.error(
        "classifier.haiku_cap_exceeded",
        today_tokens=result.today_tokens,
        cap=result.cap,
        percent=round(result.percent, 1),
    )
    if echo_fn is not None:
        echo_fn(
            f"HALT: Haiku daily cost cap exceeded "
            f"({result.today_tokens:,}/{result.cap:,} tokens today, "
            f"{result.percent:.1f}%). Classification resumes after UTC midnight."
        )

    day_iso = today_utc_iso(now)
    if not db.record_infra_alert_once(HAIKU_CAP_ALERT_KIND, day_iso):
        log.info(
            "classifier.haiku_cap_alert.already_posted",
            day=day_iso,
        )
        return False

    channel = infra_channel or resolve_infra_channel(routing_cfg)
    if channel is None:
        log.error(
            "classifier.haiku_cap_alert.no_channel",
            day=day_iso,
        )
        return False

    subject = (
        f"Haiku cost cap exceeded: {result.today_tokens:,}/"
        f"{result.cap:,} tokens today ({result.percent:.1f}%)"
    )
    body = "Classification halted until UTC midnight rollover."
    try:
        post_infra_alert(
            channel,
            subject=subject,
            body=body,
            severity="fatal",
            client=http_client,
        )
        log.info(
            "classifier.haiku_cap_alert.posted",
            channel=channel.source,
            day=day_iso,
        )
    except Exception as exc:
        log.exception(
            "classifier.haiku_cap_alert.failed",
            error=repr(exc),
            day=day_iso,
        )
    return False


# --- X (posts-read) cap ------------------------------------------------------


@dataclass(frozen=True)
class XCapCheck:
    """Result of an X cap check. Shape mirrors :class:`HaikuCapCheck`."""

    state: CapState
    today_reads: int
    cap: int

    @property
    def percent(self) -> float:
        return (self.today_reads / self.cap * 100.0) if self.cap > 0 else 0.0


def today_x_reads(db: Storage, *, now: datetime | None = None) -> int:
    """Sum of ``items_fetched`` for the ``x`` source since UTC midnight.

    Reads are recorded by :class:`sources.x.XSource` as one row per
    response. Because of upstream ``since_id`` quirks the local count
    can drift from what X bills for — the cap uses it as an upper-bound
    proxy (over-report leans toward conservative halt; under-report
    would be more concerning but hasn't been observed).
    """
    effective_now = (now or datetime.now(UTC)).astimezone(UTC)
    start_of_day = effective_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(db.sum_api_usage("x", start_of_day))


def check_x_cap(
    db: Storage,
    cap: int,
    *,
    now: datetime | None = None,
) -> XCapCheck:
    """Classify today's X reads against ``cap`` — ``ok``/``warn``/``halt``.

    A cap of 0 is "unlimited" (matches :func:`check_haiku_cap`).
    """
    total = today_x_reads(db, now=now)
    if cap <= 0:
        state: CapState = "ok"
    elif total >= cap:
        state = "halt"
    elif total >= int(cap * WARN_FRACTION):
        state = "warn"
    else:
        state = "ok"
    return XCapCheck(state=state, today_reads=total, cap=cap)


def enforce_x_cap(
    db: Storage,
    routing_cfg: RoutingConfig,
    cap: int,
    *,
    now: datetime | None = None,
    infra_channel: InfraAlertChannel | None = None,
    http_client: Any | None = None,
    echo_fn: Any = None,
) -> bool:
    """Run the X cap check and post-once infra alert. Return ``True`` to
    continue polling, ``False`` to halt.

    ``cap`` is the per-project X-reads ceiling (currently sourced from
    ``cfg.x.daily_read_cap`` in the X source YAML). Passed explicitly
    rather than read from ``routing_cfg.cost_caps.daily_x_reads`` —
    that field is legacy documentation and is not the effective cap.
    ``routing_cfg`` is still needed for infra-channel resolution.
    """
    result = check_x_cap(db, cap, now=now)

    if result.state == "warn":
        log.warning(
            "x.daily_cap_approaching",
            today_reads=result.today_reads,
            cap=result.cap,
            percent=round(result.percent, 1),
        )
        return True

    if result.state != "halt":
        return True

    log.error(
        "x.daily_cap_exceeded",
        today_reads=result.today_reads,
        cap=result.cap,
        percent=round(result.percent, 1),
    )
    if echo_fn is not None:
        echo_fn(
            f"HALT: X daily read cap exceeded "
            f"({result.today_reads:,}/{result.cap:,} reads today, "
            f"{result.percent:.1f}%). Polling resumes after UTC midnight."
        )

    day_iso = today_utc_iso(now)
    if not db.record_infra_alert_once(X_CAP_ALERT_KIND, day_iso):
        log.info("x.daily_cap_alert.already_posted", day=day_iso)
        return False

    channel = infra_channel or resolve_infra_channel(routing_cfg)
    if channel is None:
        log.error("x.daily_cap_alert.no_channel", day=day_iso)
        return False

    subject = (
        f"X daily read cap exceeded: {result.today_reads:,}/"
        f"{result.cap:,} reads today ({result.percent:.1f}%)"
    )
    body = "X polling halted until UTC midnight rollover."
    try:
        post_infra_alert(
            channel,
            subject=subject,
            body=body,
            severity="fatal",
            client=http_client,
        )
        log.info("x.daily_cap_alert.posted", channel=channel.source, day=day_iso)
    except Exception as exc:
        log.exception("x.daily_cap_alert.failed", error=repr(exc), day=day_iso)
    return False


__all__ = [
    "HAIKU_CAP_ALERT_KIND",
    "WARN_FRACTION",
    "X_CAP_ALERT_KIND",
    "CapState",
    "HaikuCapCheck",
    "XCapCheck",
    "check_haiku_cap",
    "check_x_cap",
    "enforce_haiku_cap",
    "enforce_x_cap",
    "resolve_infra_channel",
    "today_haiku_tokens",
    "today_utc_iso",
    "today_x_reads",
]
