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


def test_label_disagreements_queue_only_includes_category_mismatches(tmp_path: Path) -> None:
    root, db_path = _seed(tmp_path, n_items=3)
    labels_file = ensure_labels_file("demo", projects_root=root)

    # Pre-label all three items.
    append_label(
        labels_file,
        make_entry(item_id="hackernews:100", category="cost_complaint", urgency=8, note=None),
    )
    append_label(
        labels_file,
        make_entry(item_id="hackernews:101", category="off_topic", urgency=0, note=None),
    )
    append_label(
        labels_file,
        make_entry(item_id="hackernews:102", category="self_host_intent", urgency=7, note=None),
    )

    # Prime v1 classifications: 100 agrees, 101 disagrees, 102 has no
    # classification (should be excluded from the disagreement queue).
    with Storage(db_path) as db:
        db.save_classification(
            item_id="hackernews:100",
            category="cost_complaint",  # agrees
            urgency=8,
            reasoning="ok",
            prompt_version="v1",
            model="m",
            input_tokens=10,
            output_tokens=5,
            classified_at=datetime(2026, 4, 18, tzinfo=UTC),
            raw_response={},
        )
        db.save_classification(
            item_id="hackernews:101",
            category="cost_complaint",  # disagrees (human said off_topic)
            urgency=7,
            reasoning="mis",
            prompt_version="v1",
            model="m",
            input_tokens=10,
            output_tokens=5,
            classified_at=datetime(2026, 4, 18, tzinfo=UTC),
            raw_response={},
        )

    script = _Script(["q"])  # quit immediately — we're just checking queue size
    result = run_label(
        "demo",
        db_path,
        root,
        source=None,
        randomize=False,
        disagreements_for_version="v1",
        input_fn=script.input,
        echo_fn=script.echo,
    )
    # Only the one mismatch (101) is in the queue. 100 agrees; 102 has
    # no classification.
    assert result["total"] == 1
    # The operator's context line shows what the classifier said.
    echoed = "\n".join(script.echoed)
    assert "[classifier v1] said:" in echoed
    assert "cost_complaint" in echoed


def test_label_reconsider_queue_filters_by_category_and_urgency(tmp_path: Path) -> None:
    root, db_path = _seed(tmp_path, n_items=3)
    labels_file = ensure_labels_file("demo", projects_root=root)

    # Items 100/101/102 each labeled differently.
    append_label(
        labels_file,
        make_entry(item_id="hackernews:100", category="self_host_intent", urgency=9, note=None),
    )
    append_label(
        labels_file,
        make_entry(item_id="hackernews:101", category="self_host_intent", urgency=4, note=None),
    )
    append_label(
        labels_file,
        make_entry(item_id="hackernews:102", category="cost_complaint", urgency=8, note=None),
    )

    # --reconsider --category self_host_intent --urgency-min 7
    # should yield only item 100 (u=9). Item 101 fails urgency filter;
    # 102 fails category filter.
    script = _Script(["q"])  # quit before doing anything
    result = run_label(
        "demo",
        db_path,
        root,
        source=None,
        randomize=False,
        reconsider=True,
        reconsider_category="self_host_intent",
        reconsider_urgency_min=7,
        input_fn=script.input,
        echo_fn=script.echo,
    )
    assert result["total"] == 1


def test_label_reconsider_enter_keeps_current_and_does_not_append(tmp_path: Path) -> None:
    """Enter = keep-current. labeled.jsonl must be unchanged."""
    root, db_path = _seed(tmp_path, n_items=1)
    labels_file = ensure_labels_file("demo", projects_root=root)
    append_label(
        labels_file,
        make_entry(item_id="hackernews:100", category="self_host_intent", urgency=8, note=None),
    )
    before = labels_file.read_text(encoding="utf-8")

    script = _Script(["", "q"])  # Enter → keep; then q
    result = run_label(
        "demo",
        db_path,
        root,
        source=None,
        randomize=False,
        reconsider=True,
        reconsider_category="self_host_intent",
        input_fn=script.input,
        echo_fn=script.echo,
    )
    assert result["labeled"] == 0  # no relabels
    assert result["kept"] == 1
    # File content is byte-identical — no new line appended.
    assert labels_file.read_text(encoding="utf-8") == before


def test_label_reconsider_relabel_appends_preserving_history(tmp_path: Path) -> None:
    """Relabeling appends a new entry; the original label is retained
    in the JSONL file for audit."""
    root, db_path = _seed(tmp_path, n_items=1)
    labels_file = ensure_labels_file("demo", projects_root=root)
    original = make_entry(
        item_id="hackernews:100",
        category="self_host_intent",
        urgency=9,
        note="first take",
    )
    append_label(labels_file, original)

    script = _Script(
        [
            # Relabel: category_id 'off_topic' (or its number).
            "off_topic",
            # Urgency: empty input → use default (current = 9). Then
            # prompt for note.
            "",
            "re-examined under sharper taxonomy",
            "q",
        ]
    )
    result = run_label(
        "demo",
        db_path,
        root,
        source=None,
        randomize=False,
        reconsider=True,
        reconsider_category="self_host_intent",
        input_fn=script.input,
        echo_fn=script.echo,
    )
    assert result["labeled"] == 1  # one relabel
    assert result["kept"] == 0

    # File now has BOTH entries — append-only.
    entries = iter_label_entries(labels_file)
    assert len(entries) == 2
    cats = [e.category for e in entries]
    assert cats == ["self_host_intent", "off_topic"]
    # Default urgency carried over.
    assert entries[1].urgency == 9
    assert entries[1].note == "re-examined under sharper taxonomy"
    # Latest wins under the resolve-effective helper's logic.
    latest = max(entries, key=lambda e: e.labeled_at)
    assert latest.category == "off_topic"


def test_label_reconsider_urgency_override_accepted(tmp_path: Path) -> None:
    root, db_path = _seed(tmp_path, n_items=1)
    labels_file = ensure_labels_file("demo", projects_root=root)
    append_label(
        labels_file,
        make_entry(item_id="hackernews:100", category="self_host_intent", urgency=9, note=None),
    )

    # Relabel to off_topic with explicit urgency=2 (not the default 9).
    script = _Script(["off_topic", "2", "", "q"])
    run_label(
        "demo",
        db_path,
        root,
        source=None,
        randomize=False,
        reconsider=True,
        reconsider_category="self_host_intent",
        input_fn=script.input,
        echo_fn=script.echo,
    )
    entries = iter_label_entries(labels_file)
    latest = max(entries, key=lambda e: e.labeled_at)
    assert latest.category == "off_topic"
    assert latest.urgency == 2


def test_label_reconsider_and_disagreements_are_mutually_exclusive(tmp_path: Path) -> None:
    import typer as _typer

    root, db_path = _seed(tmp_path, n_items=1)

    import pytest

    with pytest.raises(_typer.BadParameter):
        run_label(
            "demo",
            db_path,
            root,
            source=None,
            randomize=False,
            reconsider=True,
            disagreements_for_version="v1",
            input_fn=lambda _prompt="": "q",
            echo_fn=lambda _m="": None,
        )


def test_label_disagreements_relabel_appends_new_entry(tmp_path: Path) -> None:
    root, db_path = _seed(tmp_path, n_items=1)
    labels_file = ensure_labels_file("demo", projects_root=root)

    # Prior human label: cost_complaint. Classifier said self_host_intent.
    append_label(
        labels_file,
        make_entry(item_id="hackernews:100", category="cost_complaint", urgency=8, note=None),
    )
    with Storage(db_path) as db:
        db.save_classification(
            item_id="hackernews:100",
            category="self_host_intent",
            urgency=6,
            reasoning="x",
            prompt_version="v1",
            model="m",
            input_tokens=10,
            output_tokens=5,
            classified_at=datetime(2026, 4, 18, tzinfo=UTC),
            raw_response={},
        )

    # Operator reviews the disagreement and decides the classifier was
    # right — relabels as self_host_intent.
    script = _Script(
        [
            "2",  # category index 2 (self_host_intent in CATEGORIES_YAML)
            "6",
            "classifier was right",
            "q",
        ]
    )
    run_label(
        "demo",
        db_path,
        root,
        source=None,
        randomize=False,
        disagreements_for_version="v1",
        input_fn=script.input,
        echo_fn=script.echo,
    )

    # Two entries in the file now (original + correction). Latest wins.
    entries = iter_label_entries(labels_file)
    assert len(entries) == 2
    # The newer one is the self_host_intent correction.
    latest = max(entries, key=lambda e: e.labeled_at)
    assert latest.category == "self_host_intent"
    assert latest.note == "classifier was right"
