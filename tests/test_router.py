from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from social_surveyor.config import (
    DigestConfig,
    DigestScheduleConfig,
    ImmediateConfig,
    RoutingConfig,
)
from social_surveyor.notifier import NotifierConfig
from social_surveyor.router import (
    RoutingDecision,
    decide,
    route_classifications,
    send_pending_immediate_alerts,
)
from social_surveyor.storage import Storage
from social_surveyor.types import RawItem


def _cfg(
    *,
    threshold: int = 7,
    alert_worthy: tuple[str, ...] = ("cost_complaint", "self_host_intent", "competitor_pain"),
) -> RoutingConfig:
    return RoutingConfig(
        immediate=ImmediateConfig(
            threshold_urgency=threshold,
            alert_worthy_categories=list(alert_worthy),
            webhook_secret="TEST_IMMEDIATE_WEBHOOK",
        ),
        digest=DigestConfig(
            schedule=DigestScheduleConfig(hour=9, minute=0, timezone="UTC"),
            webhook_secret="TEST_DIGEST_WEBHOOK",
            window_hours=24,
        ),
    )


def _seed_classified_item(
    db: Storage,
    *,
    item_id: str = "hackernews:100",
    category: str = "cost_complaint",
    urgency: int = 8,
) -> int:
    source, platform_id = item_id.split(":", 1)
    db.upsert_item(
        RawItem(
            source=source,
            platform_id=platform_id,
            url=f"https://ex/{platform_id}",
            title="t",
            body="b",
            author="alice",
            created_at=datetime(2026, 4, 19, 12, 0, tzinfo=UTC),
            raw_json={"id": platform_id},
        )
    )
    db.save_classification(
        item_id=item_id,
        category=category,
        urgency=urgency,
        reasoning="ok",
        prompt_version="v3",
        model="haiku",
        input_tokens=100,
        output_tokens=50,
        classified_at=datetime.now(UTC),
        raw_response={},
    )
    # Caller wants the id of the just-inserted classification.
    row = db._conn.execute(
        "SELECT id FROM classifications WHERE item_id = ? ORDER BY id DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    return int(row["id"])


# --- pure decide() -----------------------------------------------------------


def test_decide_immediate_when_alert_worthy_and_above_threshold() -> None:
    cfg = _cfg(threshold=7)
    assert decide(category="cost_complaint", urgency=7, silenced=False, cfg=cfg) == "immediate"
    assert decide(category="cost_complaint", urgency=10, silenced=False, cfg=cfg) == "immediate"


def test_decide_digest_when_below_threshold() -> None:
    cfg = _cfg(threshold=7)
    assert decide(category="cost_complaint", urgency=6, silenced=False, cfg=cfg) == "digest"


def test_decide_digest_when_category_not_alert_worthy() -> None:
    cfg = _cfg()
    # active_practitioner is explicitly NOT alert-worthy — high urgency
    # still routes to the digest per session-3 relationship-building
    # framing.
    assert decide(category="active_practitioner", urgency=9, silenced=False, cfg=cfg) == "digest"
    assert decide(category="off_topic", urgency=10, silenced=False, cfg=cfg) == "digest"


def test_decide_silenced_always_digest() -> None:
    cfg = _cfg(threshold=7)
    # Even a high-urgency alert-worthy item goes to digest if silenced.
    assert decide(category="cost_complaint", urgency=10, silenced=True, cfg=cfg) == "digest"


# --- route_classifications() end-to-end -------------------------------------


def test_route_decides_alert_for_cost_complaint_above_threshold(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        _seed_classified_item(db, item_id="hackernews:100", category="cost_complaint", urgency=8)
        decisions = route_classifications(db, _cfg())
    assert len(decisions) == 1
    d = decisions[0]
    assert d.channel == "immediate"
    assert d.silenced is False


def test_route_decides_digest_for_silenced_item(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        _seed_classified_item(db, item_id="hackernews:200", category="cost_complaint", urgency=9)
        db.silence_item("hackernews:200")
        decisions = route_classifications(db, _cfg())
    assert len(decisions) == 1
    assert decisions[0].channel == "digest"
    assert decisions[0].silenced is True


def test_route_is_idempotent(tmp_path: Path) -> None:
    """Second run finds no unrouted classifications — no duplicate alerts rows."""
    with Storage(tmp_path / "t.db") as db:
        _seed_classified_item(db, urgency=8)
        first = route_classifications(db, _cfg())
        second = route_classifications(db, _cfg())
    assert len(first) == 1
    assert second == []


def test_route_dry_run_does_not_write_alerts_rows(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        _seed_classified_item(db, urgency=8)
        decisions = route_classifications(db, _cfg(), dry_run=True)
        # A second non-dry-run sees the same classification as unrouted.
        rows_after = db._conn.execute("SELECT COUNT(*) AS c FROM alerts").fetchone()
    assert len(decisions) == 1
    assert rows_after["c"] == 0


# --- send_pending_immediate_alerts() ----------------------------------------


def test_send_pending_immediate_alerts_posts_and_marks_sent(tmp_path: Path) -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append({"url": str(request.url), "body": request.content.decode()})
        return httpx.Response(200, text="ok")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with Storage(tmp_path / "t.db") as db:
        _seed_classified_item(db, item_id="hackernews:1", category="cost_complaint", urgency=9)
        route_classifications(db, _cfg())
        sent = send_pending_immediate_alerts(
            db,
            notifier_cfg=NotifierConfig(project="demo", sv_command="sv"),
            webhook_url="https://hooks.example/A/B/C",
            client=client,
        )
        # Second pass: nothing pending; no POSTs.
        sent_again = send_pending_immediate_alerts(
            db,
            notifier_cfg=NotifierConfig(project="demo", sv_command="sv"),
            webhook_url="https://hooks.example/A/B/C",
            client=client,
        )

    client.close()
    assert len(captured) == 1
    assert captured[0]["url"] == "https://hooks.example/A/B/C"
    assert len(sent) == 1
    assert sent_again == []


def test_send_pending_immediate_alerts_leaves_sent_at_null_on_failure(tmp_path: Path) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="nope")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with Storage(tmp_path / "t.db") as db:
        _seed_classified_item(db, item_id="hackernews:2", category="cost_complaint", urgency=9)
        route_classifications(db, _cfg())
        send_pending_immediate_alerts(
            db,
            notifier_cfg=NotifierConfig(project="demo"),
            webhook_url="https://hooks.example/A/B/C",
            client=client,
        )
        row = db._conn.execute(
            "SELECT sent_at FROM alerts WHERE channel = 'immediate' LIMIT 1"
        ).fetchone()
    client.close()
    # Post failed → sent_at stays NULL for retry on the next run.
    assert row["sent_at"] is None


def test_send_pending_immediate_alerts_dry_run_does_not_post(tmp_path: Path) -> None:
    calls: list[int] = []

    def handler(_req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, text="ok")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with Storage(tmp_path / "t.db") as db:
        _seed_classified_item(db, item_id="hackernews:3", category="cost_complaint", urgency=9)
        route_classifications(db, _cfg())
        send_pending_immediate_alerts(
            db,
            notifier_cfg=NotifierConfig(project="demo"),
            webhook_url="https://hooks.example/A/B/C",
            dry_run=True,
            client=client,
        )
    client.close()
    assert calls == []


# Silence test_router as a valid test target (no side effects on import).
_ = RoutingDecision
