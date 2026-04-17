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
