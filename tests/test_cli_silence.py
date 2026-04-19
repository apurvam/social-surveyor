from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import typer

from social_surveyor.cli_silence import run_silence
from social_surveyor.labeling import append_label, ensure_labels_file, make_entry
from social_surveyor.storage import Storage
from social_surveyor.types import RawItem


def _seed(tmp_path: Path, *, n_items: int = 1) -> tuple[Path, Path]:
    root = tmp_path
    db_path = root / "data" / "demo.db"
    with Storage(db_path) as db:
        for i in range(n_items):
            db.upsert_item(
                RawItem(
                    source="hackernews",
                    platform_id=str(100 + i),
                    url=f"https://ex/{100 + i}",
                    title=f"item {i}",
                    body="b",
                    author="alice",
                    created_at=datetime(2026, 4, 1, tzinfo=UTC),
                    raw_json={"id": str(100 + i)},
                )
            )
    return root, db_path


def test_silence_inserts_row_and_is_idempotent(tmp_path: Path) -> None:
    _, db_path = _seed(tmp_path)
    echoed: list[str] = []

    # First silence: newly inserted.
    newly = run_silence("demo", db_path, item_id="hackernews:100", echo_fn=echoed.append)
    assert newly is True
    with Storage(db_path) as db:
        assert db.is_silenced("hackernews:100") is True

    # Re-silencing the same item is a no-op on the DB but still echoes.
    newly = run_silence("demo", db_path, item_id="hackernews:100", echo_fn=echoed.append)
    assert newly is False
    assert any("was already silenced" in line for line in echoed)


def test_silence_rejects_unknown_item(tmp_path: Path) -> None:
    _, db_path = _seed(tmp_path)
    with pytest.raises(typer.BadParameter, match="no item with id"):
        run_silence(
            "demo",
            db_path,
            item_id="hackernews:does-not-exist",
            echo_fn=lambda _m="": None,
        )


def test_silence_rejects_non_canonical_id(tmp_path: Path) -> None:
    _, db_path = _seed(tmp_path)
    with pytest.raises(typer.BadParameter, match="not canonical"):
        run_silence(
            "demo",
            db_path,
            item_id="no-colon-here",
            echo_fn=lambda _m="": None,
        )


def test_silence_rejects_missing_db(tmp_path: Path) -> None:
    """No DB yet means you haven't polled — fail cleanly instead of
    silently creating an empty DB and silencing a ghost."""
    with pytest.raises(typer.BadParameter, match="no DB at"):
        run_silence(
            "demo",
            tmp_path / "does-not-exist.db",
            item_id="hackernews:100",
            echo_fn=lambda _m="": None,
        )


def test_silence_and_label_are_independent(tmp_path: Path) -> None:
    """Silencing an item and labeling it should both succeed — the two
    affect different systems (router filter vs. eval ground truth)."""
    root, db_path = _seed(tmp_path)
    run_silence("demo", db_path, item_id="hackernews:100", echo_fn=lambda _m="": None)

    labels_file = ensure_labels_file("demo", projects_root=root)
    append_label(
        labels_file,
        make_entry(item_id="hackernews:100", category="cost_complaint", urgency=7, note=None),
    )

    with Storage(db_path) as db:
        assert db.is_silenced("hackernews:100") is True
    assert labels_file.read_text(encoding="utf-8").strip() != ""


def test_silence_echoes_recovery_sql(tmp_path: Path) -> None:
    """The CLI surface must advertise the DELETE to reverse a silence —
    visible-at-the-point-of-action guardrail."""
    _, db_path = _seed(tmp_path)
    echoed: list[str] = []
    run_silence("demo", db_path, item_id="hackernews:100", echo_fn=echoed.append)
    text = "\n".join(echoed)
    assert "DELETE FROM silenced_items" in text
    assert "hackernews:100" in text
