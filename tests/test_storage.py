from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from social_surveyor.storage import Storage
from social_surveyor.types import RawItem


def _item(platform_id: str = "abc123", **overrides: object) -> RawItem:
    defaults: dict[str, object] = {
        "source": "reddit",
        "platform_id": platform_id,
        "url": f"https://reddit.com/r/devops/comments/{platform_id}/x",
        "title": "A post",
        "body": "body text",
        "author": "alice",
        "created_at": datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
        "raw_json": {"id": platform_id, "subreddit": "devops"},
    }
    defaults.update(overrides)
    return RawItem(**defaults)  # type: ignore[arg-type]


def test_upsert_returns_true_on_insert_false_on_duplicate(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        assert db.upsert_item(_item("a")) is True
        assert db.upsert_item(_item("a")) is False
        assert db.count_items() == 1


def test_dedupe_is_scoped_by_source(tmp_path: Path) -> None:
    # Same platform_id on different sources should not collide.
    with Storage(tmp_path / "t.db") as db:
        assert db.upsert_item(_item("a", source="reddit")) is True
        assert db.upsert_item(_item("a", source="hackernews")) is True
        assert db.count_items() == 2


def test_get_items_returns_inserted_rows(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        db.upsert_item(_item("a", title="first"))
        db.upsert_item(_item("b", title="second"))
        rows = db.get_items()
        assert {r["title"] for r in rows} == {"first", "second"}
        assert {r["platform_id"] for r in rows} == {"a", "b"}
        # raw_json round-trips as a dict, not a JSON string
        assert all(isinstance(r["raw_json"], dict) for r in rows)


def test_get_items_filters_by_source(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        db.upsert_item(_item("a", source="reddit"))
        db.upsert_item(_item("b", source="hackernews"))
        assert db.count_items(source="reddit") == 1
        rows = db.get_items(source="reddit")
        assert len(rows) == 1
        assert rows[0]["source"] == "reddit"


def test_get_items_ordered_by_created_at_desc(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    with Storage(tmp_path / "t.db") as db:
        db.upsert_item(_item("old", created_at=now - timedelta(days=2)))
        db.upsert_item(_item("new", created_at=now))
        rows = db.get_items()
        assert [r["platform_id"] for r in rows] == ["new", "old"]


def test_schema_persists_across_connections(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    with Storage(db_path) as db:
        db.upsert_item(_item("a"))
    with Storage(db_path) as db:
        assert db.count_items() == 1


def test_get_cursor_missing_returns_none(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        assert db.get_cursor("x", "q1") is None


def test_set_and_get_cursor(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        db.set_cursor("x", "q1", "12345")
        assert db.get_cursor("x", "q1") == "12345"
        db.set_cursor("x", "q1", "67890")  # update path
        assert db.get_cursor("x", "q1") == "67890"


def test_get_cursors_returns_all_for_source(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        db.set_cursor("x", "q1", "1")
        db.set_cursor("x", "q2", "2")
        db.set_cursor("hackernews", "q3", "3")
        assert db.get_cursors("x") == {"q1": "1", "q2": "2"}
        assert db.get_cursors("hackernews") == {"q3": "3"}


def test_record_and_sum_api_usage(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        db.record_api_usage("x", "q1", 50)
        db.record_api_usage("x", "q1", 30)
        db.record_api_usage("x", "q2", 20)
        start_of_day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        assert db.sum_api_usage("x", start_of_day) == 100
        assert db.api_usage_by_query("x", start_of_day) == {"q1": 80, "q2": 20}


def test_sum_api_usage_respects_since_cutoff(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        db.record_api_usage("x", "q1", 50)
        # Everything was recorded "now"; a future cutoff sees nothing.
        future = datetime.now(UTC) + timedelta(hours=1)
        assert db.sum_api_usage("x", future) == 0


# --- classifications -----------------------------------------------------


def _save_classification(db: Storage, **overrides: object) -> None:
    defaults: dict[str, object] = {
        "item_id": "hackernews:41234567",
        "category": "cost_complaint",
        "urgency": 8,
        "reasoning": "explicit dollar amount",
        "prompt_version": "v1",
        "model": "claude-haiku-4-5-20251001",
        "input_tokens": 1200,
        "output_tokens": 80,
        "classified_at": datetime(2026, 4, 18, 10, 0, tzinfo=UTC),
        "raw_response": {"content": [{"text": "..."}], "stop_reason": "end_turn"},
    }
    defaults.update(overrides)
    db.save_classification(**defaults)  # type: ignore[arg-type]


def test_save_and_get_classification(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        _save_classification(db)
        c = db.get_classification("hackernews:41234567", "v1")
        assert c is not None
        assert c["category"] == "cost_complaint"
        assert c["urgency"] == 8
        assert c["input_tokens"] == 1200
        # raw_response round-trips as a dict, not a JSON string.
        assert isinstance(c["raw_response"], dict)
        assert c["raw_response"]["stop_reason"] == "end_turn"
        # classified_at decodes to a datetime with tzinfo.
        assert isinstance(c["classified_at"], datetime)


def test_get_classification_returns_latest_by_classified_at(tmp_path: Path) -> None:
    # Re-classifying the same item under the same prompt_version writes
    # a new row; readers want the newest.
    with Storage(tmp_path / "t.db") as db:
        old = datetime(2026, 4, 17, 9, 0, tzinfo=UTC)
        new = datetime(2026, 4, 18, 9, 0, tzinfo=UTC)
        _save_classification(db, classified_at=old, urgency=3, reasoning="first take")
        _save_classification(db, classified_at=new, urgency=9, reasoning="second take")
        c = db.get_classification("hackernews:41234567", "v1")
        assert c is not None
        assert c["urgency"] == 9
        assert c["reasoning"] == "second take"


def test_get_classification_returns_none_when_missing(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        assert db.get_classification("does:not_exist", "v1") is None


def test_list_classifications_returns_all_versions_newest_first(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        _save_classification(
            db,
            prompt_version="v1",
            classified_at=datetime(2026, 4, 17, tzinfo=UTC),
        )
        _save_classification(
            db,
            prompt_version="v2",
            classified_at=datetime(2026, 4, 18, tzinfo=UTC),
        )
        rows = db.list_classifications("hackernews:41234567")
        assert [r["prompt_version"] for r in rows] == ["v2", "v1"]


def test_count_classifications_filters(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        _save_classification(db, item_id="a:1", prompt_version="v1", category="cost_complaint")
        _save_classification(db, item_id="a:2", prompt_version="v1", category="off_topic")
        _save_classification(db, item_id="a:3", prompt_version="v2", category="cost_complaint")
        assert db.count_classifications() == 3
        assert db.count_classifications(prompt_version="v1") == 2
        assert db.count_classifications(category="cost_complaint") == 2
        assert db.count_classifications(prompt_version="v1", category="cost_complaint") == 1


def test_get_unclassified_items_excludes_classified_for_that_version(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        db.upsert_item(_item("a"))
        db.upsert_item(_item("b"))
        _save_classification(db, item_id="reddit:a", prompt_version="v1")
        # 'b' has no classification; 'a' has one under v1 only.
        v1_unclassified = db.get_unclassified_items("v1")
        assert [f"{r['source']}:{r['platform_id']}" for r in v1_unclassified] == ["reddit:b"]
        # Under v2, both items are still unclassified.
        v2_unclassified = db.get_unclassified_items("v2")
        assert {f"{r['source']}:{r['platform_id']}" for r in v2_unclassified} == {
            "reddit:a",
            "reddit:b",
        }


def test_get_unclassified_items_honors_limit(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        for i in range(5):
            db.upsert_item(_item(f"id{i}"))
        assert len(db.get_unclassified_items("v1", limit=2)) == 2


def test_record_api_usage_accepts_token_counts(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        db.record_api_usage("anthropic", "v1", 1, input_tokens=1200, output_tokens=80)
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        # items_fetched still totals as before.
        assert db.sum_api_usage("anthropic", start) == 1
        # Token totals are reported separately.
        assert db.sum_api_tokens("anthropic", start) == (1200, 80)


def test_sum_api_tokens_treats_null_as_zero(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        # A non-LLM source (X) leaves token columns NULL.
        db.record_api_usage("x", "q1", 100)
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        assert db.sum_api_tokens("x", start) == (0, 0)


def test_migration_adds_token_columns_to_pre_session3_db(tmp_path: Path) -> None:
    # Simulate a DB created before Session 3: api_usage exists without
    # input_tokens/output_tokens columns. Opening it with the current
    # Storage should add them transparently without breaking existing
    # rows.
    import sqlite3

    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as raw:
        raw.execute(
            """
            CREATE TABLE api_usage (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                source        TEXT NOT NULL,
                query_name    TEXT NOT NULL,
                items_fetched INTEGER NOT NULL,
                fetched_at    TEXT NOT NULL
            )
            """
        )
        raw.execute(
            "INSERT INTO api_usage (source, query_name, items_fetched, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            ("x", "q1", 50, datetime(2026, 4, 1, tzinfo=UTC).isoformat()),
        )
        raw.commit()

    # Opening migrates.
    with Storage(db_path) as db:
        cols = {r["name"] for r in db._conn.execute("PRAGMA table_info(api_usage)").fetchall()}
        assert "input_tokens" in cols
        assert "output_tokens" in cols
        # Pre-migration rows keep items_fetched and get NULL token counts;
        # sum_api_tokens treats NULL as 0.
        start = datetime(2026, 3, 1, tzinfo=UTC)
        assert db.sum_api_usage("x", start) == 50
        assert db.sum_api_tokens("x", start) == (0, 0)


def test_silence_item_is_idempotent(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        assert db.silence_item("hackernews:1") is True
        assert db.silence_item("hackernews:1") is False  # second call is no-op
        assert db.is_silenced("hackernews:1") is True
        assert db.is_silenced("hackernews:does-not-exist") is False


def test_silenced_since_filters_by_window(tmp_path: Path) -> None:
    """silenced_since returns only items whose silenced_at is on/after the cutoff.

    Used by the digest to render the 🔕 marker inside the 24h window
    without accreting older silences forever.
    """
    import sqlite3

    db_path = tmp_path / "t.db"
    with Storage(db_path) as db:
        db.silence_item("x:1")
        db.silence_item("x:2")
        # Rewrite one row's timestamp to a week ago so the window excludes it.
        week_ago = datetime.now(UTC) - timedelta(days=7)
        with sqlite3.connect(db_path) as raw:
            raw.execute(
                "UPDATE silenced_items SET silenced_at = ? WHERE item_id = ?",
                (week_ago.isoformat(), "x:1"),
            )
            raw.commit()

        cutoff = datetime.now(UTC) - timedelta(days=1)
        assert db.silenced_since(cutoff) == {"x:2"}


# --- alerts windowing ---------------------------------------------------
#
# list_alerts_in_window is the foundation the digest runs on top of:
# - include_unsent=True must return only pending alerts (no re-posting
#   what was delivered in a prior cycle)
# - include_unsent=False must return only delivered alerts within the
#   window (no pending rows leaking into "what shipped" audits)
#
# These are disjoint sets by design — see the docstring on the method.


def _seed_alert_row(
    db: Storage,
    *,
    item_id: str = "hackernews:1",
    channel: str = "digest",
    category: str = "cost_complaint",
    urgency: int = 8,
    sent_at: datetime | None = None,
) -> int:
    """Seed the item, classification, and alert needed for a window query."""
    source, platform_id = item_id.split(":", 1)
    db.upsert_item(
        RawItem(
            source=source,
            platform_id=platform_id,
            url=f"https://ex/{platform_id}",
            title=f"t-{platform_id}",
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
        reasoning="r",
        prompt_version="v1",
        model="haiku",
        input_tokens=1,
        output_tokens=1,
        classified_at=datetime.now(UTC),
        raw_response={},
    )
    classification_id = db.get_classification(item_id, "v1")["id"]  # type: ignore[index]
    return db.record_alert(
        item_id=item_id,
        classification_id=classification_id,
        channel=channel,
        sent_at=sent_at,
    )


def test_list_alerts_in_window_pending_only_returns_unsent(tmp_path: Path) -> None:
    """include_unsent=True returns only alerts where sent_at IS NULL and
    queued_at is within the window. Already-sent alerts whose sent_at
    falls in the window (e.g., delivered in a prior digest cycle that
    still overlaps today's rolling window) must NOT be re-surfaced —
    that was the duplication bug that had items shipping in two
    consecutive daily digests.
    """
    db_path = tmp_path / "t.db"
    with Storage(db_path) as db:
        pending_id = _seed_alert_row(db, item_id="hackernews:pending", sent_at=None)
        _seed_alert_row(
            db,
            item_id="hackernews:sent",
            sent_at=datetime.now(UTC) - timedelta(hours=1),
        )

        window_start = datetime.now(UTC) - timedelta(hours=24)
        pending = db.list_alerts_in_window(
            channel="digest",
            since=window_start,
            include_unsent=True,
        )
        assert [r["alert_id"] for r in pending] == [pending_id]
        assert pending[0]["item_id"] == "hackernews:pending"


def test_list_alerts_in_window_sent_only_returns_already_delivered(tmp_path: Path) -> None:
    """include_unsent=False is the companion mode: strictly the alerts
    that landed in Slack within the window (audit, inspection)."""
    db_path = tmp_path / "t.db"
    with Storage(db_path) as db:
        _seed_alert_row(db, item_id="hackernews:pending", sent_at=None)
        sent_id = _seed_alert_row(
            db,
            item_id="hackernews:sent",
            sent_at=datetime.now(UTC) - timedelta(hours=1),
        )

        window_start = datetime.now(UTC) - timedelta(hours=24)
        delivered = db.list_alerts_in_window(
            channel="digest",
            since=window_start,
            include_unsent=False,
        )
        assert [r["alert_id"] for r in delivered] == [sent_id]


def test_list_alerts_in_window_consecutive_digest_cycles_do_not_duplicate(
    tmp_path: Path,
) -> None:
    """The regression this file most wants to guard against: yesterday's
    digest posted item A; today's digest runs 24h later with the same
    24h window; A's sent_at is exactly 24h old, at the window boundary.
    The pending query must skip A (already delivered) and return only
    items queued since A shipped.
    """
    db_path = tmp_path / "t.db"
    with Storage(db_path) as db:
        # A shipped in the previous digest cycle.
        _seed_alert_row(
            db,
            item_id="hackernews:A",
            sent_at=datetime.now(UTC) - timedelta(hours=23, minutes=59),
        )
        # B was queued an hour ago, still pending.
        b_id = _seed_alert_row(db, item_id="hackernews:B", sent_at=None)

        window_start = datetime.now(UTC) - timedelta(hours=24)
        pending = db.list_alerts_in_window(
            channel="digest",
            since=window_start,
            include_unsent=True,
        )
        assert [r["alert_id"] for r in pending] == [b_id]


def test_list_alerts_in_window_respects_queued_at_for_unsent(tmp_path: Path) -> None:
    """An unsent alert queued before the window begins must not leak
    into a pending query — even though sent_at is NULL, queued_at gates
    visibility. Prevents a since=<future> filter from returning the
    full unsent backlog."""
    import sqlite3

    db_path = tmp_path / "t.db"
    with Storage(db_path) as db:
        alert_id = _seed_alert_row(db, item_id="hackernews:old", sent_at=None)
        # Backdate queued_at to before the window.
        with sqlite3.connect(db_path) as raw:
            raw.execute(
                "UPDATE alerts SET queued_at = ? WHERE id = ?",
                ((datetime.now(UTC) - timedelta(days=7)).isoformat(), alert_id),
            )
            raw.commit()

        window_start = datetime.now(UTC) - timedelta(hours=24)
        pending = db.list_alerts_in_window(
            channel="digest",
            since=window_start,
            include_unsent=True,
        )
        assert pending == []
