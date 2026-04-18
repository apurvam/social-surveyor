from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from social_surveyor.cli_label import _resolve_category, run_label
from social_surveyor.config import CategoryConfig, load_categories
from social_surveyor.labeling import (
    append_label,
    ensure_labels_file,
    iter_label_entries,
    make_entry,
)
from social_surveyor.storage import Storage
from social_surveyor.types import RawItem

CATEGORIES_YAML = """
version: 1
categories:
  - id: cost_complaint
    label: "Cost complaint"
    description: "Complaining about price."
  - id: self_host_intent
    label: "Self-host intent"
    description: "Asking about self-hosting."
  - id: off_topic
    label: "Off topic"
    description: "Not relevant."
urgency_scale:
  - range: [0, 5]
    meaning: "low"
  - range: [6, 10]
    meaning: "high"
"""


def _seed(tmp_path: Path, n_items: int) -> tuple[Path, Path]:
    (tmp_path / "demo").mkdir(parents=True, exist_ok=True)
    (tmp_path / "demo" / "categories.yaml").write_text(CATEGORIES_YAML)
    db_path = tmp_path / "data" / "demo.db"
    with Storage(db_path) as db:
        for i in range(n_items):
            db.upsert_item(
                RawItem(
                    source="hackernews",
                    platform_id=str(100 + i),
                    url=f"https://news.ycombinator.com/item?id={100 + i}",
                    title=f"Test story {i}",
                    body=f"Body of story {i}",
                    author=f"user{i}",
                    created_at=datetime(2026, 4, 1, 12, i, tzinfo=UTC),
                    raw_json={"group_key": "hackernews:test"},
                )
            )
    return tmp_path, db_path


class _Script:
    """Deterministic input_fn / echo_fn pair for driving run_label."""

    def __init__(self, inputs: list[str]) -> None:
        self.inputs = list(inputs)
        self.echoed: list[str] = []

    def input(self, prompt: str = "") -> str:
        if not self.inputs:
            raise RuntimeError(f"ran out of scripted inputs at prompt: {prompt!r}")
        return self.inputs.pop(0)

    def echo(self, text: str = "") -> None:
        self.echoed.append(text)


def test_resolve_category_accepts_number_and_id() -> None:
    cfg = CategoryConfig.model_validate(
        {
            "categories": [
                {"id": "cost_complaint", "label": "x", "description": "y"},
                {"id": "off_topic", "label": "x", "description": "y"},
            ],
            "urgency_scale": [{"range": [0, 10], "meaning": "all"}],
        }
    )
    assert _resolve_category("1", cfg) == "cost_complaint"
    assert _resolve_category("2", cfg) == "off_topic"
    assert _resolve_category("cost_complaint", cfg) == "cost_complaint"
    assert _resolve_category("COST_COMPLAINT", cfg) == "cost_complaint"
    assert _resolve_category("3", cfg) is None  # out of range
    assert _resolve_category("banana", cfg) is None


def test_label_writes_jsonl_per_decision(tmp_path: Path) -> None:
    root, db_path = _seed(tmp_path, n_items=3)
    # Label the first item using the numeric shortcut, skip the second, quit on the third.
    script = _Script(
        [
            "1",  # category: cost_complaint
            "7",  # urgency
            "costs too much",  # note
            "s",  # skip
            "q",  # quit
        ]
    )

    result = run_label(
        "demo",
        db_path,
        root,
        source=None,
        randomize=False,
        input_fn=script.input,
        echo_fn=script.echo,
    )

    assert result["labeled"] == 1
    assert result["skipped"] == 1

    labels_file = root / "demo" / "evals" / "labeled.jsonl"
    entries = iter_label_entries(labels_file)
    assert len(entries) == 1
    assert entries[0].category == "cost_complaint"
    assert entries[0].urgency == 7
    assert entries[0].note == "costs too much"


def test_label_resume_skips_already_labeled(tmp_path: Path) -> None:
    root, db_path = _seed(tmp_path, n_items=3)
    # Pre-label item 100 so the queue starts at item 101.
    labels_file = ensure_labels_file("demo", projects_root=root)
    append_label(
        labels_file,
        make_entry(item_id="hackernews:100", category="cost_complaint", urgency=7, note=None),
    )

    script = _Script(["q"])  # quit immediately
    result = run_label(
        "demo",
        db_path,
        root,
        source=None,
        randomize=False,
        input_fn=script.input,
        echo_fn=script.echo,
    )

    # Queue had only 2 unlabeled items (101, 102), not 3.
    assert result["total"] == 2


def test_label_back_step_removes_last_line_and_resurfaces_item(tmp_path: Path) -> None:
    root, db_path = _seed(tmp_path, n_items=2)

    script = _Script(
        [
            "1",  # item 1: category
            "5",  # urgency
            "",  # no note
            "b",  # go back
            # Item 1 is re-presented
            "2",  # different category this time
            "9",
            "new note",
            "q",
        ]
    )

    run_label(
        "demo",
        db_path,
        root,
        source=None,
        randomize=False,
        input_fn=script.input,
        echo_fn=script.echo,
    )

    labels_file = root / "demo" / "evals" / "labeled.jsonl"
    entries = iter_label_entries(labels_file)
    assert len(entries) == 1  # only the re-labeled version
    assert entries[0].category == "self_host_intent"
    assert entries[0].urgency == 9
    assert entries[0].note == "new note"


def test_label_rejects_bad_urgency_and_reprompts(tmp_path: Path) -> None:
    root, db_path = _seed(tmp_path, n_items=1)
    script = _Script(
        [
            "1",  # category
            "banana",  # bad urgency
            "99",  # out of range
            "4",  # finally valid
            "",  # no note
            "q",
        ]
    )
    result = run_label(
        "demo",
        db_path,
        root,
        source=None,
        randomize=False,
        input_fn=script.input,
        echo_fn=script.echo,
    )

    assert result["labeled"] == 1
    # Echo history should contain the two error messages.
    echoed_text = "\n".join(script.echoed)
    assert "urgency must be an integer" in echoed_text
    assert "urgency out of range" in echoed_text


def test_label_reopens_loaded_categories(tmp_path: Path) -> None:
    """Sanity: the loop reads categories.yaml, not a hardcoded list."""
    root, _ = _seed(tmp_path, n_items=0)
    cfg = load_categories("demo", projects_root=root)
    assert [c.id for c in cfg.categories] == ["cost_complaint", "self_host_intent", "off_topic"]
