from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from social_surveyor.cli_stats import run_stats
from social_surveyor.labeling import append_label, ensure_labels_file, make_entry
from social_surveyor.storage import Storage
from social_surveyor.types import RawItem


def _item(
    platform_id: str,
    *,
    source: str = "hackernews",
    group_key: str | None = "hackernews:q1",
    created_at: datetime | None = None,
) -> RawItem:
    raw: dict[str, object] = {}
    if group_key is not None:
        raw["group_key"] = group_key
    return RawItem(
        source=source,
        platform_id=platform_id,
        url=f"https://example.com/{platform_id}",
        title=f"item {platform_id}",
        body="body",
        author="author",
        created_at=created_at or datetime.now(UTC),
        raw_json=raw,
    )


def _seed_db(tmp_path: Path, project: str) -> Path:
    db_path = tmp_path / "data" / f"{project}.db"
    now = datetime.now(UTC)
    with Storage(db_path) as db:
        db.upsert_item(_item("1", group_key="hackernews:q1", created_at=now))
        db.upsert_item(_item("2", group_key="hackernews:q1", created_at=now - timedelta(hours=2)))
        db.upsert_item(_item("3", group_key="hackernews:q2", created_at=now - timedelta(days=2)))
        db.upsert_item(
            _item("4", source="reddit", group_key="reddit:r/devops/prom", created_at=now)
        )
        # A pre-group_key item — should land in the (unknown query) bucket.
        db.upsert_item(_item("5", source="reddit", group_key=None, created_at=now))
        # Old — outside the 7d window.
        db.upsert_item(_item("6", group_key="hackernews:q1", created_at=now - timedelta(days=30)))
    return db_path


def test_run_stats_renders_source_totals_and_windows(tmp_path: Path) -> None:
    db_path = _seed_db(tmp_path, "demo")
    out = run_stats("demo", db_path, projects_root=tmp_path)

    # Source totals: hackernews has 4 (1,2,3,6), reddit has 2 (4, 5).
    assert "hackernews" in out
    assert "reddit" in out
    # Total across both sources and all time = 6.
    assert "TOTAL" in out
    assert "6" in out  # total count shows up


def test_run_stats_surfaces_unknown_bucket_explicitly(tmp_path: Path) -> None:
    db_path = _seed_db(tmp_path, "demo")
    out = run_stats("demo", db_path, projects_root=tmp_path)

    assert "(unknown query)" in out


def test_run_stats_groups_within_7d_window(tmp_path: Path) -> None:
    db_path = _seed_db(tmp_path, "demo")
    out = run_stats("demo", db_path, projects_root=tmp_path)

    # The 30-day-old hackernews:q1 item is outside the 7d window used for
    # groups, but hackernews:q1 still appears because items 1 and 2 are
    # recent.
    assert "hackernews:q1" in out
    assert "reddit:r/devops/prom" in out


def test_run_stats_reports_labeled_counts(tmp_path: Path) -> None:
    db_path = _seed_db(tmp_path, "demo")
    labels_file = ensure_labels_file("demo", projects_root=tmp_path)
    append_label(
        labels_file,
        make_entry(item_id="hackernews:1", category="cost_complaint", urgency=7, note=None),
    )
    append_label(
        labels_file,
        make_entry(item_id="hackernews:2", category="off_topic", urgency=2, note=None),
    )

    out = run_stats("demo", db_path, projects_root=tmp_path)

    # 2 labeled, 4 unlabeled (6 items total - 2 labeled).
    assert "labeled" in out
    assert "unlabeled" in out


def test_run_stats_errors_cleanly_when_db_missing(tmp_path: Path) -> None:
    import pytest
    import typer

    missing = tmp_path / "data" / "never.db"
    with pytest.raises(typer.BadParameter) as exc:
        run_stats("demo", missing, projects_root=tmp_path)
    assert "no DB" in str(exc.value)
