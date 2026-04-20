"""`social-surveyor route` — decide + (optionally) send immediate alerts.

Two phases in one command:

1. Route every unrouted classification (writes alerts rows with
   ``sent_at=NULL``).
2. Send pending immediate alerts via the configured webhook.

``--dry-run`` short-circuits both phases: prints what would be decided
and which immediate alerts would be sent, without writing to the DB
or hitting Slack. Useful for seeing the effect of a routing.yaml
change before committing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from .config import ConfigError, load_categories, load_routing_config
from .notifier import NotifierConfig
from .router import (
    RoutingDecision,
    route_classifications,
    send_pending_immediate_alerts,
)
from .secrets import SecretNotFoundError, resolve_secret
from .storage import Storage


def run_route(
    project: str,
    db_path: Path,
    projects_root: Path,
    *,
    dry_run: bool,
    echo_fn: Any = typer.echo,
    http_client: Any = None,
) -> dict[str, int]:
    """Route pending classifications and send pending immediate alerts.

    Returns a counter dict for the caller (tests, future run loop).
    """
    try:
        routing_cfg = load_routing_config(project, projects_root=projects_root)
    except ConfigError as e:
        raise typer.BadParameter(str(e)) from None

    if not db_path.is_file():
        raise typer.BadParameter(f"no DB at {db_path} yet — run a poll first")

    try:
        categories = load_categories(project, projects_root=projects_root)
        category_labels = {c.id: c.label for c in categories.categories}
    except ConfigError:
        category_labels = {}

    notifier_cfg = NotifierConfig(
        project=project,
        category_labels=category_labels,
    )

    with Storage(db_path) as db:
        decisions = route_classifications(db, routing_cfg, dry_run=dry_run)
        _summarize_decisions(decisions, echo_fn=echo_fn, dry_run=dry_run)

        # Only resolve the webhook when we actually have something to
        # send. This keeps `route` working on test projects with no
        # immediate-channel traffic and no webhook configured.
        pending_rows = db.list_pending_alerts("immediate")
        webhook_url = ""
        if pending_rows and not dry_run:
            try:
                webhook_url = resolve_secret(routing_cfg.immediate.webhook_secret)
            except SecretNotFoundError as e:
                raise typer.BadParameter(str(e)) from None

        pending = send_pending_immediate_alerts(
            db,
            notifier_cfg=notifier_cfg,
            webhook_url=webhook_url,
            dry_run=dry_run,
            client=http_client,
        )

    counts = {
        "decided": len(decisions),
        "immediate": sum(1 for d in decisions if d.channel == "immediate"),
        "digest": sum(1 for d in decisions if d.channel == "digest"),
        "silenced_skipped": sum(1 for d in decisions if d.silenced),
        "pending_immediate_sent": 0 if dry_run else len(pending),
        "pending_immediate_would_send": len(pending) if dry_run else 0,
    }
    if dry_run:
        echo_fn(
            f"dry-run: would route {counts['decided']} classifications "
            f"({counts['immediate']} immediate, {counts['digest']} digest, "
            f"{counts['silenced_skipped']} silenced) and send "
            f"{counts['pending_immediate_would_send']} immediate alerts."
        )
    else:
        echo_fn(
            f"routed {counts['decided']} classifications "
            f"({counts['immediate']} immediate, {counts['digest']} digest, "
            f"{counts['silenced_skipped']} silenced). "
            f"Sent {counts['pending_immediate_sent']} pending immediate alerts."
        )
    return counts


def _summarize_decisions(
    decisions: list[RoutingDecision],
    *,
    echo_fn: Any,
    dry_run: bool,
) -> None:
    """Render a compact table of this run's routing decisions."""
    if not decisions:
        return
    prefix = "would route" if dry_run else "routed"
    for d in decisions:
        mark = "🔕" if d.silenced else ("🔔" if d.channel == "immediate" else "📰")
        echo_fn(f"  {prefix} {mark} {d.item_id}  {d.category}  u={d.urgency}  → {d.channel}")


__all__ = ["run_route"]
