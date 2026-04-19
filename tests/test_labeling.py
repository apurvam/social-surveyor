from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from social_surveyor.labeling import (
    LabelEntry,
    LabelFileError,
    append_label,
    count_labeled_ids,
    ensure_labels_file,
    iter_label_entries,
    labeled_ids,
    labels_path,
    make_entry,
    resolve_effective_labels,
)


def _entry(
    item_id: str = "hackernews:123",
    category: str = "cost_complaint",
    urgency: int = 7,
    labeled_at: datetime | None = None,
) -> LabelEntry:
    return LabelEntry(
        item_id=item_id,
        category=category,
        urgency=urgency,
        note=None,
        labeled_at=labeled_at or datetime.now(UTC),
    )


def test_labels_path_is_project_scoped(tmp_path: Path) -> None:
    p = labels_path("demo", projects_root=tmp_path)
    assert p == tmp_path / "demo" / "evals" / "labeled.jsonl"


def test_ensure_labels_file_creates_parent_and_touches_file(tmp_path: Path) -> None:
    p = ensure_labels_file("demo", projects_root=tmp_path)
    assert p.exists()
    assert p.parent.exists()
    assert p.read_text() == ""


def test_append_read_round_trip(tmp_path: Path) -> None:
    path = ensure_labels_file("demo", projects_root=tmp_path)
    e1 = _entry("hackernews:1")
    e2 = _entry("reddit:t3_abc", category="self_host_intent")
    append_label(path, e1)
    append_label(path, e2)

    entries = iter_label_entries(path)
    assert len(entries) == 2
    assert entries[0].item_id == "hackernews:1"
    assert entries[1].category == "self_host_intent"


def test_labeled_ids_returns_unique_items(tmp_path: Path) -> None:
    path = ensure_labels_file("demo", projects_root=tmp_path)
    append_label(path, _entry("hackernews:1"))
    append_label(path, _entry("hackernews:1"))  # re-label same item
    append_label(path, _entry("x:2"))

    ids = labeled_ids(path)
    assert ids == {"hackernews:1", "x:2"}
    assert count_labeled_ids(path) == 2


def test_resolve_effective_labels_latest_wins_two_entries(tmp_path: Path) -> None:
    """A second label for the same item_id with a later timestamp wins;
    the raw file retains both entries for audit."""
    path = ensure_labels_file("demo", projects_root=tmp_path)
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    original = _entry("hackernews:1", category="cost_complaint", urgency=8, labeled_at=t0)
    correction = _entry(
        "hackernews:1",
        category="off_topic",
        urgency=1,
        labeled_at=t0 + timedelta(hours=1),
    )
    append_label(path, original)
    append_label(path, correction)

    # Raw file has both.
    raw = iter_label_entries(path)
    assert len(raw) == 2
    # Effective view collapses to latest.
    effective = resolve_effective_labels(raw)
    assert set(effective.keys()) == {"hackernews:1"}
    assert effective["hackernews:1"].category == "off_topic"
    assert effective["hackernews:1"].urgency == 1


def test_resolve_effective_labels_latest_wins_three_entries(tmp_path: Path) -> None:
    """Three labels at distinct timestamps — the latest is authoritative
    regardless of file order."""
    path = ensure_labels_file("demo", projects_root=tmp_path)
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    first = _entry("x:42", category="neutral_discussion", urgency=2, labeled_at=t0)
    middle = _entry(
        "x:42",
        category="cost_complaint",
        urgency=7,
        labeled_at=t0 + timedelta(days=1),
    )
    last = _entry(
        "x:42",
        category="self_host_intent",
        urgency=6,
        labeled_at=t0 + timedelta(days=2),
    )
    # Append in non-chronological order to confirm the resolver relies
    # on labeled_at, not file position.
    append_label(path, middle)
    append_label(path, first)
    append_label(path, last)

    effective = resolve_effective_labels(iter_label_entries(path))
    assert effective["x:42"].category == "self_host_intent"
    assert effective["x:42"].urgency == 6
    # And the raw file still has all three.
    assert len(iter_label_entries(path)) == 3


def test_resolve_effective_labels_handles_empty_and_single(tmp_path: Path) -> None:
    path = ensure_labels_file("demo", projects_root=tmp_path)
    assert resolve_effective_labels(iter_label_entries(path)) == {}

    append_label(path, _entry("a:1"))
    effective = resolve_effective_labels(iter_label_entries(path))
    assert list(effective.keys()) == ["a:1"]


def test_iter_label_entries_raises_on_malformed(tmp_path: Path) -> None:
    path = ensure_labels_file("demo", projects_root=tmp_path)
    append_label(path, _entry("a:1"))
    with path.open("a") as f:
        f.write('{"item_id": "a:2"}\n')  # missing required fields

    with pytest.raises(LabelFileError) as exc:
        iter_label_entries(path)
    assert ":2:" in str(exc.value)  # points at the bad line number


def test_make_entry_stamps_now_utc() -> None:
    e = make_entry(item_id="a:1", category="cost_complaint", urgency=5, note="x")
    assert e.labeled_at.tzinfo is not None
    # Within a few seconds of now.
    delta = (datetime.now(UTC) - e.labeled_at).total_seconds()
    assert abs(delta) < 5


def test_urgency_out_of_range_rejected() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LabelEntry(
            item_id="a:1",
            category="x",
            urgency=11,  # out of [0, 10]
            note=None,
            labeled_at=datetime.now(UTC),
        )
