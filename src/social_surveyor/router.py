"""Route classifications to either the immediate channel or the digest.

Decision rules (from routing.yaml):

1. If the item is silenced, record a digest-channel alerts row but
   never send immediately. The digest builder will still filter older
   silences out of its output — recording the row here preserves the
   "was routed" audit trail without spamming the user.
2. If the classification's category is in ``alert_worthy_categories``
   AND ``urgency >= threshold_urgency``, channel = ``immediate``.
3. Otherwise, channel = ``digest``.

The router records rows with ``sent_at=NULL``. A separate send step
(``send_pending_immediate_alerts``) posts pending immediate alerts to
Slack and flips their ``sent_at`` to now. This split keeps dry-run
trivially safe — nothing hits Slack unless you explicitly call the
sender.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from .config import RoutingConfig
from .notifier import NotifierConfig, NotifierItem, build_immediate_alert, post_to_slack
from .storage import Storage

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RoutingDecision:
    """One router outcome, suitable for logging and dry-run output."""

    item_id: str
    classification_id: int
    category: str
    urgency: int
    channel: str  # 'immediate' or 'digest'
    silenced: bool


def decide(
    *,
    category: str,
    urgency: int,
    silenced: bool,
    cfg: RoutingConfig,
) -> str:
    """Pure routing decision. Returns ``'immediate'`` or ``'digest'``.

    Silenced items always go to the digest channel — they never fire
    an immediate alert, but the digest-side filter decides whether to
    show them with a 🔕 marker or hide them entirely.
    """
    if silenced:
        return "digest"
    if (
        category in cfg.immediate.alert_worthy_categories
        and urgency >= cfg.immediate.threshold_urgency
    ):
        return "immediate"
    return "digest"


def route_classifications(
    db: Storage,
    cfg: RoutingConfig,
    *,
    dry_run: bool = False,
) -> list[RoutingDecision]:
    """Route every classification that doesn't yet have an alerts row.

    When ``dry_run=True`` no DB writes happen — the decisions are
    returned for inspection. Otherwise each decision writes a
    corresponding alerts row with ``sent_at=NULL``; the immediate-channel
    rows are picked up by :func:`send_pending_immediate_alerts`.
    """
    pending = db.list_unrouted_classifications()
    decisions: list[RoutingDecision] = []
    for row in pending:
        item_id = row["item_id"]
        silenced = db.is_silenced(item_id)
        channel = decide(
            category=row["category"],
            urgency=int(row["urgency"]),
            silenced=silenced,
            cfg=cfg,
        )
        d = RoutingDecision(
            item_id=item_id,
            classification_id=int(row["id"]),
            category=row["category"],
            urgency=int(row["urgency"]),
            channel=channel,
            silenced=silenced,
        )
        decisions.append(d)
        if not dry_run:
            db.record_alert(
                item_id=item_id,
                classification_id=int(row["id"]),
                channel=channel,
            )
        log.info(
            "router.decided",
            item_id=item_id,
            category=row["category"],
            urgency=int(row["urgency"]),
            silenced=silenced,
            channel=channel,
            dry_run=dry_run,
        )
    return decisions


def send_pending_immediate_alerts(
    db: Storage,
    *,
    notifier_cfg: NotifierConfig,
    webhook_url: str,
    dry_run: bool = False,
    client: Any | None = None,
) -> list[dict[str, Any]]:
    """Post every pending immediate alert to Slack; mark them sent.

    Returns the list of pending alert row dicts that were considered
    (for logging / dry-run display). Callers can inspect the return
    to see what would have been sent.

    No retries beyond what httpx does internally — if a POST fails, the
    alert row stays pending and the next ``route`` invocation picks it
    up. This is deliberate: Slack outages shouldn't lose alerts.
    """
    pending = db.list_pending_alerts("immediate")
    for row in pending:
        item = _notifier_item_from_row(row)
        payload = build_immediate_alert(item, notifier_cfg)
        if dry_run:
            log.info(
                "router.dry_run.would_send",
                item_id=row["item_id"],
                category=row["category"],
                urgency=row["urgency"],
            )
            continue
        try:
            post_to_slack(payload, webhook_url, client=client)
        except Exception as exc:
            log.exception(
                "router.immediate.post_failed",
                item_id=row["item_id"],
                error=repr(exc),
            )
            # Leave sent_at=NULL so the next run retries.
            continue
        db.mark_alert_sent(int(row["alert_id"]), datetime.now(UTC))
        log.info(
            "router.immediate.sent",
            item_id=row["item_id"],
            category=row["category"],
            urgency=row["urgency"],
            alert_id=row["alert_id"],
        )
    return pending


def _notifier_item_from_row(row: dict[str, Any]) -> NotifierItem:
    """Adapt a storage row dict to :class:`NotifierItem` input shape."""
    return NotifierItem(
        item_id=row["item_id"],
        source=row["source"],
        category=row["category"],
        urgency=int(row["urgency"]),
        title=row.get("title") or "",
        body=row.get("body"),
        author=row.get("author"),
        url=row.get("url"),
        created_at=row["created_at"],
        reasoning=row.get("reasoning"),
    )


__all__ = [
    "RoutingDecision",
    "decide",
    "route_classifications",
    "send_pending_immediate_alerts",
]
