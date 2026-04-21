from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer

from social_surveyor.cli_digest import run_digest
from social_surveyor.cli_route import run_route
from social_surveyor.storage import Storage
from social_surveyor.types import RawItem
from tests.test_cli_classify import _write_project_configs


def _add_routing_yaml(projects_root: Path) -> None:
    (projects_root / "demo" / "routing.yaml").write_text(
        """
version: 1
immediate:
  threshold_urgency: 7
  alert_worthy_categories:
    - cost_complaint
    - self_host_intent
    - competitor_pain
  webhook_secret: TEST_DEMO_IMMEDIATE_WEBHOOK
digest:
  schedule:
    hour: 9
    minute: 0
    timezone: UTC
  webhook_secret: TEST_DEMO_DIGEST_WEBHOOK
  window_hours: 24
cost_caps:
  daily_haiku_tokens: 500000
""",
        encoding="utf-8",
    )


def _seed_item_and_classification(
    db: Storage,
    *,
    item_id: str,
    category: str,
    urgency: int,
    title: str = "t",
) -> None:
    source, platform_id = item_id.split(":", 1)
    db.upsert_item(
        RawItem(
            source=source,
            platform_id=platform_id,
            url=f"https://ex/{platform_id}",
            title=title,
            body="body",
            author="alice",
            created_at=datetime.now(UTC) - timedelta(minutes=30),
            raw_json={"id": platform_id},
        )
    )
    db.save_classification(
        item_id=item_id,
        category=category,
        urgency=urgency,
        reasoning="reason",
        prompt_version="v3",
        model="haiku",
        input_tokens=100,
        output_tokens=50,
        classified_at=datetime.now(UTC),
        raw_response={},
    )


def test_digest_dry_run_prints_block_kit_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects_root = _write_project_configs(tmp_path)
    _add_routing_yaml(projects_root)
    db_path = tmp_path / "data" / "demo.db"

    # Both webhooks mocked so the route pass can post its immediate alert.
    monkeypatch.setenv("TEST_DEMO_IMMEDIATE_WEBHOOK", "https://hooks.example/immediate")

    with Storage(db_path) as db:
        _seed_item_and_classification(
            db, item_id="hackernews:1", category="cost_complaint", urgency=8
        )
        _seed_item_and_classification(db, item_id="hackernews:2", category="off_topic", urgency=0)

    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200, text="ok")))
    run_route(
        "demo",
        db_path,
        projects_root,
        dry_run=False,
        http_client=client,
        echo_fn=lambda _m="": None,
    )
    client.close()

    echoed: list[str] = []
    result = run_digest(
        "demo",
        db_path,
        projects_root,
        dry_run=True,
        echo_fn=echoed.append,
    )
    joined = "\n".join(echoed)
    # The JSON payload is the first echoed block.
    payload = json.loads(joined.split("\n\n")[0] if joined else "{}")
    assert "blocks" in payload
    assert result["posted"] is False
    # The cost_complaint u=8 item routes to immediate and lands in the
    # immediate Slack channel; the digest does not recap it. Only the
    # off_topic (digest-channel) item surfaces in the digest render.
    assert result["items"] == 1


def test_digest_posts_to_slack_and_marks_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects_root = _write_project_configs(tmp_path)
    _add_routing_yaml(projects_root)
    db_path = tmp_path / "data" / "demo.db"

    monkeypatch.setenv("TEST_DEMO_DIGEST_WEBHOOK", "https://hooks.example/digest")
    monkeypatch.setenv("TEST_DEMO_IMMEDIATE_WEBHOOK", "https://hooks.example/immediate")

    posted: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        posted.append({"url": str(req.url), "body": req.content.decode()})
        return httpx.Response(200, text="ok")

    with Storage(db_path) as db:
        _seed_item_and_classification(
            db,
            item_id="hackernews:1",
            category="off_topic",  # non-alert-worthy → digest channel
            urgency=1,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    # Route first so an alerts row exists on the digest channel.
    run_route(
        "demo",
        db_path,
        projects_root,
        dry_run=False,
        http_client=client,
        echo_fn=lambda _m="": None,
    )
    run_digest(
        "demo",
        db_path,
        projects_root,
        dry_run=False,
        http_client=client,
        echo_fn=lambda _m="": None,
    )
    client.close()

    # One POST to the digest webhook.
    digest_posts = [p for p in posted if "digest" in p["url"]]
    assert len(digest_posts) == 1
    # Digest alerts row now has sent_at set.
    with Storage(db_path) as db:
        rows = db._conn.execute("SELECT sent_at FROM alerts WHERE channel = 'digest'").fetchall()
    assert all(r["sent_at"] is not None for r in rows)


def test_digest_category_inspection_prints_to_stdout_not_slack(tmp_path: Path) -> None:
    projects_root = _write_project_configs(tmp_path)
    _add_routing_yaml(projects_root)
    db_path = tmp_path / "data" / "demo.db"

    with Storage(db_path) as db:
        for i in range(3):
            _seed_item_and_classification(
                db,
                item_id=f"hackernews:{i}",
                category="active_practitioner",
                urgency=5 + i,
                title=f"item {i}",
            )
    # Route so alerts rows exist.
    run_route(
        "demo",
        db_path,
        projects_root,
        dry_run=False,
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, text="ok"))
        ),
        echo_fn=lambda _m="": None,
    )

    echoed: list[str] = []
    result = run_digest(
        "demo",
        db_path,
        projects_root,
        dry_run=False,
        category="active_practitioner",
        echo_fn=echoed.append,
    )
    # Nothing posted (no HTTP call needed — Slack path isn't taken).
    assert result["posted"] is False
    text = "\n".join(echoed)
    assert "category=active_practitioner" in text
    assert "item 2" in text


def test_digest_dry_run_does_not_require_webhook(tmp_path: Path) -> None:
    """Dry-run must work even when the env var isn't set — it's the
    format-iteration loop and shouldn't block on production config."""
    projects_root = _write_project_configs(tmp_path)
    _add_routing_yaml(projects_root)
    db_path = tmp_path / "data" / "demo.db"

    with Storage(db_path) as db:
        _seed_item_and_classification(db, item_id="hackernews:1", category="off_topic", urgency=0)

    os.environ.pop("TEST_DEMO_DIGEST_WEBHOOK", None)
    os.environ.pop("TEST_DEMO_IMMEDIATE_WEBHOOK", None)

    # Should not raise — dry-run doesn't resolve the webhook secret.
    result = run_digest(
        "demo",
        db_path,
        projects_root,
        dry_run=True,
        echo_fn=lambda _m="": None,
    )
    assert result["posted"] is False


def test_digest_since_filter_is_parsed(tmp_path: Path) -> None:
    """The since filter narrows the window. Empty window → empty digest payload."""
    projects_root = _write_project_configs(tmp_path)
    _add_routing_yaml(projects_root)
    db_path = tmp_path / "data" / "demo.db"

    with Storage(db_path) as db:
        _seed_item_and_classification(db, item_id="hackernews:1", category="off_topic", urgency=0)
    run_route(
        "demo",
        db_path,
        projects_root,
        dry_run=False,
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, text="ok"))
        ),
        echo_fn=lambda _m="": None,
    )

    # Since 10 years ago — everything included.
    wide = run_digest(
        "demo",
        db_path,
        projects_root,
        dry_run=True,
        since=datetime.now(UTC) - timedelta(days=365 * 10),
        echo_fn=lambda _m="": None,
    )
    assert wide["items"] >= 1

    # Since tomorrow — nothing included.
    narrow = run_digest(
        "demo",
        db_path,
        projects_root,
        dry_run=True,
        since=datetime.now(UTC) + timedelta(days=1),
        echo_fn=lambda _m="": None,
    )
    assert narrow["items"] == 0


def test_digest_does_not_re_include_items_from_previous_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two consecutive digest cycles must not ship the same item twice.

    Regression guard for the SQL OR-branch bug: before the fix, any
    digest-channel alert whose sent_at fell inside the current 24h
    window was re-included, so every item got posted in the digest
    that picked it up AND the one 24h later. The behavior contract:
    once an item ships in a digest, it's gone from future digests.
    """
    projects_root = _write_project_configs(tmp_path)
    _add_routing_yaml(projects_root)
    db_path = tmp_path / "data" / "demo.db"

    monkeypatch.setenv("TEST_DEMO_DIGEST_WEBHOOK", "https://hooks.example/digest")
    monkeypatch.setenv("TEST_DEMO_IMMEDIATE_WEBHOOK", "https://hooks.example/immediate")

    with Storage(db_path) as db:
        _seed_item_and_classification(
            db, item_id="hackernews:A", category="off_topic", urgency=1, title="A-shipped"
        )

    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200, text="ok")))
    # Route so an alerts row exists on the digest channel.
    run_route(
        "demo",
        db_path,
        projects_root,
        dry_run=False,
        http_client=client,
        echo_fn=lambda _m="": None,
    )
    # First digest cycle: ships A, marks sent.
    first = run_digest(
        "demo",
        db_path,
        projects_root,
        dry_run=False,
        http_client=client,
        echo_fn=lambda _m="": None,
    )
    client.close()
    assert first["items"] == 1
    assert first["marked_sent"] == 1

    # Second digest cycle a little later (still inside the same 24h
    # rolling window as the first cycle — this is the exact condition
    # that used to trigger the duplication).
    client2 = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200, text="ok")))
    second = run_digest(
        "demo",
        db_path,
        projects_root,
        dry_run=True,  # dry so no extra Slack call needed
        http_client=client2,
        echo_fn=lambda _m="": None,
    )
    client2.close()
    assert second["items"] == 0


def test_digest_category_inspection_shows_both_sent_and_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator inspection via `--category` must surface both currently-
    pending and previously-delivered alerts in the window. The digest
    render path hides delivered items (already in Slack), but the
    inspection path is the local source of truth — it has to see
    everything regardless of which Slack channel the item went to.
    """
    projects_root = _write_project_configs(tmp_path)
    _add_routing_yaml(projects_root)
    db_path = tmp_path / "data" / "demo.db"
    monkeypatch.setenv("TEST_DEMO_DIGEST_WEBHOOK", "https://hooks.example/digest")
    monkeypatch.setenv("TEST_DEMO_IMMEDIATE_WEBHOOK", "https://hooks.example/immediate")

    with Storage(db_path) as db:
        # Two digest-channel items in the same category.
        _seed_item_and_classification(
            db, item_id="hackernews:shipped", category="off_topic", urgency=0, title="shipped item"
        )
        _seed_item_and_classification(
            db, item_id="hackernews:pending", category="off_topic", urgency=0, title="pending item"
        )

    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200, text="ok")))
    run_route(
        "demo",
        db_path,
        projects_root,
        dry_run=False,
        http_client=client,
        echo_fn=lambda _m="": None,
    )

    # Ship one item in a prior digest cycle, leave the other pending.
    with Storage(db_path) as db:
        db._conn.execute(
            "UPDATE alerts SET sent_at = ? WHERE item_id = ?",
            (datetime.now(UTC).isoformat(), "hackernews:shipped"),
        )
        db._conn.commit()

    echoed: list[str] = []
    result = run_digest(
        "demo",
        db_path,
        projects_root,
        dry_run=False,
        category="off_topic",
        echo_fn=echoed.append,
    )
    assert result["posted"] is False
    text = "\n".join(echoed)
    # Both items visible; sent state tagged so the operator can tell them apart.
    assert "shipped item" in text
    assert "pending item" in text
    assert "(sent)" in text
    assert "(pending)" in text


def test_digest_footer_renders_authoritative_x_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end mock: when X is configured and X_BEARER_TOKEN is set,
    the digest footer pulls usage from X's authoritative endpoint —
    threaded through the injected http_client so tests don't hit the
    network. Single MockTransport handles both Slack and X hosts.
    """
    projects_root = _write_project_configs(tmp_path)
    _add_routing_yaml(projects_root)
    # Configure X in the project so _resolve_x_usage doesn't short-circuit.
    (projects_root / "demo" / "sources" / "x.yaml").write_text(
        "queries:\n  - name: q1\n    query: 'test'\ndaily_read_cap: 500\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("X_BEARER_TOKEN", "AAAAtest-token")
    monkeypatch.setenv("TEST_DEMO_DIGEST_WEBHOOK", "https://hooks.example/digest")
    monkeypatch.setenv("TEST_DEMO_IMMEDIATE_WEBHOOK", "https://hooks.example/immediate")

    db_path = tmp_path / "data" / "demo.db"
    with Storage(db_path) as db:
        _seed_item_and_classification(db, item_id="hackernews:1", category="off_topic", urgency=0)
    run_route(
        "demo",
        db_path,
        projects_root,
        dry_run=False,
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, text="ok"))
        ),
        echo_fn=lambda _m="": None,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.x.com" and request.url.path == "/2/usage/tweets":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "project_usage": 143,
                        "project_cap": 10_000,
                        "cap_reset_day": 21,
                    }
                },
            )
        # Slack or anything else
        return httpx.Response(200, text="ok")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    echoed: list[str] = []
    try:
        run_digest(
            "demo",
            db_path,
            projects_root,
            dry_run=True,  # stdout-only, no Slack POST either way
            echo_fn=echoed.append,
            http_client=client,
        )
    finally:
        client.close()

    payload_text = "\n".join(echoed).split("\n\n")[0]
    payload = json.loads(payload_text)
    # Footer is the last section block — authoritative X usage rendered.
    footer_text = next(
        b["text"]["text"]
        for b in reversed(payload["blocks"])
        if b.get("type") == "section" and "items labeled" in b.get("text", {}).get("text", "")
    )
    assert "143/10,000 posts" in footer_text
    assert "resets in 21 days" in footer_text
    # And the local-estimate $ is gone — replaced by authoritative consumption.
    assert "$0.72" not in footer_text  # no synthesized X dollar figure


def test_digest_requires_routing_yaml(tmp_path: Path) -> None:
    projects_root = _write_project_configs(tmp_path)
    # _write_project_configs now emits a default routing.yaml so the
    # classify path can load cost-caps; for this test we want the
    # missing-file path, so drop it back off.
    (projects_root / "demo" / "routing.yaml").unlink()
    db_path = tmp_path / "data" / "demo.db"
    with Storage(db_path) as db:
        _seed_item_and_classification(db, item_id="hackernews:1", category="off_topic", urgency=0)
    with pytest.raises(typer.BadParameter, match=r"routing\.yaml"):
        run_digest(
            "demo",
            db_path,
            projects_root,
            dry_run=True,
            echo_fn=lambda _m="": None,
        )
