from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from social_surveyor.cli_explain import run_explain
from social_surveyor.labeling import append_label, labels_path, make_entry
from social_surveyor.storage import Storage
from social_surveyor.types import RawItem
from tests.test_cli_classify import _write_project_configs


def test_explain_surfaces_item_label_and_classifications(tmp_path: Path) -> None:
    projects_root = _write_project_configs(tmp_path)
    db_path = tmp_path / "data" / "demo.db"
    labels_file = labels_path("demo", projects_root=projects_root)
    labels_file.parent.mkdir(parents=True, exist_ok=True)

    with Storage(db_path) as db:
        db.upsert_item(
            RawItem(
                source="hackernews",
                platform_id="42",
                url="https://hn/42",
                title="Datadog vs self-hosted",
                body="Spent $80k last quarter.",
                author="alice",
                created_at=datetime(2026, 4, 1, tzinfo=UTC),
                raw_json={"id": "42", "_tags": ["story"]},
            )
        )
        append_label(
            labels_file,
            make_entry(
                item_id="hackernews:42",
                category="cost_complaint",
                urgency=8,
                note="explicit dollar amount",
            ),
        )
        db.save_classification(
            item_id="hackernews:42",
            category="self_host_intent",
            urgency=6,
            reasoning="user describes self-hosting plans",
            prompt_version="v1",
            model="claude-haiku-4-5-20251001",
            input_tokens=1200,
            output_tokens=40,
            classified_at=datetime(2026, 4, 18, tzinfo=UTC),
            raw_response={"content": [{"text": "..."}]},
        )

    echoed: list[str] = []
    run_explain(
        "demo",
        db_path,
        projects_root,
        item_id="hackernews:42",
        echo_fn=echoed.append,
    )
    output = "\n".join(echoed)
    assert "Datadog vs self-hosted" in output
    assert "Spent $80k last quarter." in output
    assert "cost_complaint" in output  # human label
    assert "self_host_intent" in output  # model prediction
    assert "reasoning: user describes self-hosting plans" in output
    assert "reconstructed prompt" in output


def test_explain_missing_item_raises(tmp_path: Path) -> None:
    import typer

    projects_root = _write_project_configs(tmp_path)
    db_path = tmp_path / "data" / "demo.db"
    # Create empty DB.
    with Storage(db_path):
        pass

    import pytest

    with pytest.raises(typer.BadParameter):
        run_explain(
            "demo",
            db_path,
            projects_root,
            item_id="hackernews:does_not_exist",
            echo_fn=lambda _m: None,
        )


def test_explain_latest_label_wins(tmp_path: Path) -> None:
    """When the same item is labeled twice, explain shows the latest."""
    projects_root = _write_project_configs(tmp_path)
    db_path = tmp_path / "data" / "demo.db"
    labels_file = labels_path("demo", projects_root=projects_root)
    labels_file.parent.mkdir(parents=True, exist_ok=True)

    with Storage(db_path) as db:
        db.upsert_item(
            RawItem(
                source="hackernews",
                platform_id="99",
                url="https://hn/99",
                title="t",
                body="b",
                author=None,
                created_at=datetime(2026, 4, 1, tzinfo=UTC),
                raw_json={},
            )
        )
        append_label(
            labels_file,
            make_entry(
                item_id="hackernews:99",
                category="off_topic",
                urgency=0,
                note=None,
            ),
        )
        # Sleep a bit is overkill; just create one with a forward-dated
        # labeled_at to guarantee ordering in the JSONL.
        import json as _json

        entry = make_entry(
            item_id="hackernews:99",
            category="cost_complaint",
            urgency=9,
            note="updated take",
        )
        d = entry.model_dump()
        d["labeled_at"] = datetime(2099, 1, 1, tzinfo=UTC).isoformat()
        labels_file.write_text(labels_file.read_text() + _json.dumps(d) + "\n", encoding="utf-8")

    echoed: list[str] = []
    run_explain(
        "demo",
        db_path,
        projects_root,
        item_id="hackernews:99",
        echo_fn=echoed.append,
    )
    output = "\n".join(echoed)
    # Latest label is cost_complaint; off_topic should not appear as the
    # effective label.
    effective_block = output.split("=== effective human label ===")[1].split("=== classifications")[
        0
    ]
    assert "cost_complaint" in effective_block
    assert "off_topic" not in effective_block
