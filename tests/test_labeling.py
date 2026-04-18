from __future__ import annotations

from datetime import UTC, datetime
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
    pop_last_label,
)


def _entry(item_id: str = "hackernews:123", category: str = "cost_complaint") -> LabelEntry:
    return LabelEntry(
        item_id=item_id,
        category=category,
        urgency=7,
        note=None,
        labeled_at=datetime.now(UTC),
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


def test_pop_last_label_truncates_last_line(tmp_path: Path) -> None:
    path = ensure_labels_file("demo", projects_root=tmp_path)
    append_label(path, _entry("a:1"))
    append_label(path, _entry("a:2"))
    append_label(path, _entry("a:3"))

    popped = pop_last_label(path)
    assert popped is not None
    assert popped.item_id == "a:3"

    remaining = iter_label_entries(path)
    assert [e.item_id for e in remaining] == ["a:1", "a:2"]


def test_pop_last_label_noop_on_missing_file(tmp_path: Path) -> None:
    assert pop_last_label(tmp_path / "nope.jsonl") is None


def test_pop_last_label_removes_corrupt_trailing_line(tmp_path: Path) -> None:
    path = ensure_labels_file("demo", projects_root=tmp_path)
    append_label(path, _entry("a:1"))
    # Simulate partial write / corruption on the last line.
    with path.open("a") as f:
        f.write("not-json-at-all\n")

    popped = pop_last_label(path)
    assert popped is None  # corrupt line returned as "no valid pop"
    # But the corrupt line is gone, so the operator isn't stuck.
    assert [e.item_id for e in iter_label_entries(path)] == ["a:1"]


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
