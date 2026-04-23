"""`social-surveyor digest` — build and send the daily Slack digest.

Three modes share this command:

- **Slack mode** (default): build the digest from alerts in the last
  ``window_hours`` and POST to the digest webhook. Marks digest-channel
  alerts as sent so tomorrow's run doesn't re-include them.
- **``--dry-run``**: build the Block Kit payload and print the JSON to
  stdout. No Slack call, no DB state change. The JSON can be pasted
  into Slack's Block Kit Builder for formatting review.
- **``--category <cat>``**: skip Slack entirely. Print a full listing
  of items in that category to stdout, respecting ``--since`` and
  ``--limit``. This is the inspection command the main digest's
  overflow hint points to.

Cost and accuracy footer numbers come from the api_usage table + the
labels file (for total_labeled) + an optional last-eval JSON export
(for accuracy_pct). Missing any piece is fine — the footer degrades
gracefully.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import typer

from .cli_eval import HAIKU_INPUT_USD_PER_MTOK, HAIKU_OUTPUT_USD_PER_MTOK
from .config import ConfigError, load_categories, load_project_config, load_routing_config
from .labeling import count_labeled_ids, labels_path
from .notifier import (
    DigestStats,
    NotifierConfig,
    NotifierItem,
    XUsageSnapshot,
    build_digest,
    post_to_slack,
)
from .secrets import SecretNotFoundError, resolve_secret
from .sources.x import fetch_x_usage
from .storage import Storage


def run_digest(
    project: str,
    db_path: Path,
    projects_root: Path,
    *,
    dry_run: bool,
    category: str | None = None,
    since: datetime | None = None,
    limit: int | None = None,
    echo_fn: Any = typer.echo,
    http_client: Any = None,
) -> dict[str, Any]:
    """Build and (optionally) post the daily digest.

    Returns a result dict for programmatic callers (tests, scheduler).
    """
    try:
        routing_cfg = load_routing_config(project, projects_root=projects_root)
    except ConfigError as e:
        raise typer.BadParameter(str(e)) from None

    if not db_path.is_file():
        raise typer.BadParameter(f"no DB at {db_path} yet — run a poll first")

    window_start = since or (datetime.now(UTC) - timedelta(hours=routing_cfg.digest.window_hours))

    # Load category labels from categories.yaml so the digest can
    # show human-friendly category names ("Observability cost
    # complaint") instead of snake_case ids.
    try:
        categories = load_categories(project, projects_root=projects_root)
        category_labels = {c.id: c.label for c in categories.categories}
    except ConfigError:
        # Categories missing is a different error path (classifier
        # can't load either); degrade to empty map so the digest
        # still ships with id-form labels.
        category_labels = {}

    notifier_cfg = NotifierConfig(
        project=project,
        category_labels=category_labels,
    )

    with Storage(db_path) as db:
        # Category-inspection mode: stdout only, never Slack. Keeps the
        # Slack channel quiet while the operator browses.
        if category is not None:
            return _run_category_inspection(
                db,
                category=category,
                since=window_start,
                limit=limit,
                echo_fn=echo_fn,
            )

        items = _collect_digest_items(db, window_start=window_start)
        stats = _compute_digest_stats(
            db,
            project,
            projects_root,
            window_start=window_start,
            http_client=http_client,
        )

        payload = build_digest(items, stats, notifier_cfg)

        if dry_run:
            echo_fn(json.dumps(payload, indent=2, default=str))
            return {
                "posted": False,
                "items": len(items),
                "payload": payload,
            }

        # Skip the Slack post when there's nothing to show. Multi-project
        # deploys otherwise flood the digest channel with "no new items"
        # cards that add no signal; liveness should come from a dedicated
        # /health check (session 5c), not from a daily empty digest.
        if not items:
            echo_fn(f"digest skipped: 0 items in the last {routing_cfg.digest.window_hours}h")
            return {
                "posted": False,
                "items": 0,
                "skipped_empty": True,
            }

        try:
            webhook_url = resolve_secret(routing_cfg.digest.webhook_secret)
        except SecretNotFoundError as e:
            raise typer.BadParameter(str(e)) from None

        post_to_slack(payload, webhook_url, client=http_client)

        # Mark every digest-channel alert we just rendered as sent so
        # the next digest doesn't duplicate. Immediate-channel alerts
        # keep their existing sent_at.
        sent_at = datetime.now(UTC)
        marked = 0
        for row in db.list_alerts_in_window(
            channel="digest",
            since=window_start,
            include_unsent=True,
        ):
            if row.get("sent_at") is None:
                db.mark_alert_sent(int(row["alert_id"]), sent_at)
                marked += 1

    echo_fn(f"posted digest: {len(items)} items, {marked} digest-channel alerts marked sent")
    return {
        "posted": True,
        "items": len(items),
        "marked_sent": marked,
    }


# --- collection ---------------------------------------------------------------


def _collect_digest_items(
    db: Storage,
    *,
    window_start: datetime,
) -> list[NotifierItem]:
    """Pull digest-channel items pending for this cycle.

    Returns ``channel='digest'`` alerts that are still unsent and were
    queued within the window. Items posted in a prior digest are
    filtered out at the SQL layer (see
    :meth:`Storage.list_alerts_in_window`) so each item ships in at
    most one digest. Immediate-channel items are not included — they
    were delivered to the immediate Slack channel and are consumed.

    Silenced items are hidden *unless* their silence is within the
    window, in which case they render with a marker.
    """
    silenced_in_window = db.silenced_since(window_start)

    digest_rows = db.list_alerts_in_window(
        channel="digest",
        since=window_start,
        include_unsent=True,
    )

    items: list[NotifierItem] = []
    for row in digest_rows:
        item_id = row["item_id"]
        is_silenced_now = db.is_silenced(item_id)
        if is_silenced_now and item_id not in silenced_in_window:
            # Older silence — hide this item entirely. The user
            # already decided they don't want to see it; don't spam
            # them by re-litigating with the marker.
            continue
        items.append(_row_to_notifier_item(row, silenced=is_silenced_now))

    return items


def _row_to_notifier_item(
    row: dict[str, Any],
    *,
    silenced: bool = False,
) -> NotifierItem:
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
        silenced=silenced,
    )


# --- stats --------------------------------------------------------------------


def _compute_digest_stats(
    db: Storage,
    project: str,
    projects_root: Path,
    *,
    window_start: datetime,
    http_client: Any = None,
) -> DigestStats:
    """Compute cost + accuracy footer data. Degrades gracefully.

    X usage comes from ``/2/usage/tweets`` (authoritative), not a local
    counter multiplied by an assumed rate. A fetch failure (auth blip,
    rate limit, network) leaves ``x_usage=None`` so the footer renders
    a short fallback; the rest of the digest is unaffected.

    ``http_client`` is the single ``httpx.Client`` threaded down from
    :func:`run_digest` — shared with the Slack poster. Tests pass a
    ``MockTransport`` client and handle both hosts in one handler so
    no branch in this codepath reaches live network.
    """
    del window_start  # accepted for signature symmetry with collectors
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    haiku_in, haiku_out = db.sum_api_tokens("anthropic", today_start)
    haiku_cost = (
        haiku_in / 1_000_000 * HAIKU_INPUT_USD_PER_MTOK
        + haiku_out / 1_000_000 * HAIKU_OUTPUT_USD_PER_MTOK
    )

    total_labeled = count_labeled_ids(labels_path(project, projects_root=projects_root))
    accuracy_pct = _latest_accuracy_pct(projects_root / project)

    x_configured = _project_has_x(project, projects_root)
    x_usage = (
        _resolve_x_usage(project, projects_root, http_client=http_client)
        if x_configured
        else None
    )

    return DigestStats(
        day=datetime.now(UTC).date(),
        haiku_cost_usd=haiku_cost,
        total_labeled=total_labeled,
        accuracy_pct=accuracy_pct,
        x_configured=x_configured,
        x_usage=x_usage,
    )


def _project_has_x(project: str, projects_root: Path) -> bool:
    """True when the project's ``sources/x.yaml`` exists and loads
    successfully. Centralized so the footer's "X is configured" check
    and the usage-fetch gate are in one place.
    """
    try:
        cfg = load_project_config(project, projects_root=projects_root)
    except ConfigError:
        return False
    return cfg.x is not None


def _resolve_x_usage(
    project: str,
    projects_root: Path,
    *,
    http_client: Any = None,
) -> XUsageSnapshot | None:
    """Pull the current X-project usage. ``None`` when the API call
    fails or ``X_BEARER_TOKEN`` is missing. Caller guarantees X is
    configured for this project (see :func:`_project_has_x`).

    ``http_client`` (optional) is forwarded to :func:`fetch_x_usage`
    so tests can mock the call through a shared transport. ``None``
    lets ``fetch_x_usage`` create a short-lived client per call (prod
    path: once per digest cycle).
    """
    import os

    token = os.environ.get("X_BEARER_TOKEN")
    if not token:
        return None
    usage = fetch_x_usage(token, client=http_client)
    if usage is None:
        return None
    return XUsageSnapshot(
        project_usage=usage.project_usage,
        project_cap=usage.project_cap,
        cap_reset_day=usage.cap_reset_day,
    )


def _latest_accuracy_pct(project_dir: Path) -> float | None:
    """Scan the project dir for an eval export JSON and return its accuracy.

    We don't persist eval results in the DB today — they're written
    as JSON exports by ``eval --export``. The digest footer reads
    those opportunistically. If none exist, the footer just omits
    the accuracy figure.
    """
    exports = sorted(project_dir.glob("eval_*.json"), reverse=True)
    if not exports:
        return None
    try:
        data = json.loads(exports[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    overall = data.get("metrics", {}).get("overall_accuracy", {})
    acc = overall.get("accuracy")
    return acc * 100 if isinstance(acc, (int, float)) else None


# --- category inspection ------------------------------------------------------


def _run_category_inspection(
    db: Storage,
    *,
    category: str,
    since: datetime,
    limit: int | None,
    echo_fn: Any,
) -> dict[str, Any]:
    """Print every item in ``category`` within the window to stdout.

    Aggregates every alert that landed in ``[since, now]``, regardless
    of which Slack channel it went to and regardless of whether it was
    already posted. Each item is tagged with its sent state so the
    operator can tell "this is still pending" from "this shipped at
    09:02 last Tuesday."
    """
    # sent + pending, across both channels — this is the operator's
    # "what happened in this category" lookup, so we want the union.
    sent_immediate = db.list_alerts_in_window(
        channel="immediate", since=since, include_unsent=False
    )
    sent_digest = db.list_alerts_in_window(channel="digest", since=since, include_unsent=False)
    pending_digest = db.list_alerts_in_window(channel="digest", since=since, include_unsent=True)
    rows = [r for r in (sent_immediate + sent_digest + pending_digest) if r["category"] == category]
    rows.sort(
        key=lambda r: (-(int(r["urgency"])), -(r["created_at"].timestamp())),
    )
    if limit is not None:
        rows = rows[:limit]

    echo_fn(f"category={category}  items={len(rows)}  since={since.isoformat()}")
    echo_fn("-" * 72)
    for r in rows:
        title = (r.get("title") or "(no title)")[:100]
        author = r.get("author") or "unknown"
        state = "sent" if r.get("sent_at") is not None else "pending"
        echo_fn(
            f"u={r['urgency']}  {r['item_id']}  [{r['source']}]  ({state})  {title!r} by {author}"
        )
        url = r.get("url")
        if url:
            echo_fn(f"    {url}")
    return {
        "posted": False,
        "items": len(rows),
        "category": category,
    }


__all__ = ["run_digest"]
