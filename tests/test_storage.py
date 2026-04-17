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
