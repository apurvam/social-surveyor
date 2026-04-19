from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from social_surveyor.cli_eval import run_eval
from social_surveyor.labeling import append_label, labels_path, make_entry
from social_surveyor.storage import Storage
from social_surveyor.types import RawItem
from tests.test_classifier import FakeClient, FakeResponse, _body_after_prefill
from tests.test_cli_classify import _write_project_configs


def _seed_item_and_label(
    db: Storage,
    labels_file: Path,
    item_id: str,
    *,
    title: str,
    label_category: str,
    label_urgency: int,
) -> None:
    source, platform_id = item_id.split(":", 1)
    db.upsert_item(
        RawItem(
            source=source,
            platform_id=platform_id,
            url=f"https://ex/{platform_id}",
            title=title,
            body=f"body for {title}",
            author="alice",
            created_at=datetime(2026, 4, 1, tzinfo=UTC),
            raw_json={"id": platform_id},
        )
    )
    append_label(
        labels_file,
        make_entry(
            item_id=item_id,
            category=label_category,
            urgency=label_urgency,
            note=None,
        ),
    )


def _prime_classification(
    db: Storage,
    item_id: str,
    prompt_version: str,
    *,
    category: str,
    urgency: int,
) -> None:
    db.save_classification(
        item_id=item_id,
        category=category,
        urgency=urgency,
        reasoning="primed",
        prompt_version=prompt_version,
        model="claude-haiku-4-5-20251001",
        input_tokens=100,
        output_tokens=50,
        classified_at=datetime.now(UTC),
        raw_response={"stop_reason": "end_turn"},
    )


def test_eval_warm_cache_zero_api_calls(tmp_path: Path) -> None:
    projects_root = _write_project_configs(tmp_path)
    db_path = tmp_path / "data" / "demo.db"
    labels_file = labels_path("demo", projects_root=projects_root)
    labels_file.parent.mkdir(parents=True, exist_ok=True)

    with Storage(db_path) as db:
        _seed_item_and_label(
            db,
            labels_file,
            "hackernews:hn1",
            title="Datadog pain",
            label_category="cost_complaint",
            label_urgency=8,
        )
        _seed_item_and_label(
            db,
            labels_file,
            "hackernews:hn2",
            title="Weather post",
            label_category="off_topic",
            label_urgency=1,
        )
        # Both pre-classified under v1 (one correct, one wrong).
        _prime_classification(db, "hackernews:hn1", "v1", category="cost_complaint", urgency=8)
        _prime_classification(db, "hackernews:hn2", "v1", category="off_topic", urgency=1)

    echoed: list[str] = []
    client = FakeClient([])  # zero queued responses → any API call would assert
    result = run_eval(
        "demo",
        db_path,
        projects_root,
        prompt_version_override=None,
        verbose=False,
        export_path=None,
        client=client,
        echo_fn=echoed.append,
        progress_every=0,
    )
    assert result["run_cost"]["newly_classified"] == 0
    assert result["run_cost"]["cached"] == 2
    assert len(client.messages.calls) == 0
    # Headline metric shows up.
    output = "\n".join(echoed)
    assert "Overall category accuracy:" in output
    assert "Alert-worthy category accuracy:" in output
    assert "Per-category (P/R/F1):" in output
    assert "Confusion matrix" in output
    assert "Stop criteria status:" in output


def test_eval_cold_start_classifies_missing_items(tmp_path: Path) -> None:
    projects_root = _write_project_configs(tmp_path)
    db_path = tmp_path / "data" / "demo.db"
    labels_file = labels_path("demo", projects_root=projects_root)
    labels_file.parent.mkdir(parents=True, exist_ok=True)

    with Storage(db_path) as db:
        _seed_item_and_label(
            db,
            labels_file,
            "hackernews:cold1",
            title="x",
            label_category="cost_complaint",
            label_urgency=8,
        )

    client = FakeClient([FakeResponse(_body_after_prefill(urgency=8))])
    result = run_eval(
        "demo",
        db_path,
        projects_root,
        prompt_version_override=None,
        verbose=False,
        export_path=None,
        client=client,
        echo_fn=lambda _msg: None,
        progress_every=0,
    )
    assert result["run_cost"]["newly_classified"] == 1
    assert result["run_cost"]["cached"] == 0
    assert len(client.messages.calls) == 1


def test_eval_version_exact_match(tmp_path: Path) -> None:
    """Classifications under v1 must NOT satisfy an eval for v2. The
    harness must classify fresh for v2 even if v1 data exists."""
    projects_root = _write_project_configs(tmp_path)
    db_path = tmp_path / "data" / "demo.db"
    labels_file = labels_path("demo", projects_root=projects_root)
    labels_file.parent.mkdir(parents=True, exist_ok=True)

    with Storage(db_path) as db:
        _seed_item_and_label(
            db,
            labels_file,
            "hackernews:x1",
            title="t",
            label_category="cost_complaint",
            label_urgency=8,
        )
        # v1 classification exists but the eval asks for v2.
        _prime_classification(db, "hackernews:x1", "v1", category="cost_complaint", urgency=8)

    client = FakeClient([FakeResponse(_body_after_prefill())])
    result = run_eval(
        "demo",
        db_path,
        projects_root,
        prompt_version_override="v2",
        verbose=False,
        export_path=None,
        client=client,
        echo_fn=lambda _msg: None,
        progress_every=0,
    )
    # One API call despite v1 cache — because v2 has no cache.
    assert result["run_cost"]["newly_classified"] == 1
    assert result["run_cost"]["cached"] == 0


def test_eval_export_writes_json(tmp_path: Path) -> None:
    projects_root = _write_project_configs(tmp_path)
    db_path = tmp_path / "data" / "demo.db"
    labels_file = labels_path("demo", projects_root=projects_root)
    labels_file.parent.mkdir(parents=True, exist_ok=True)

    with Storage(db_path) as db:
        _seed_item_and_label(
            db,
            labels_file,
            "hackernews:e1",
            title="t",
            label_category="cost_complaint",
            label_urgency=8,
        )
        _prime_classification(
            db, "hackernews:e1", "v1", category="off_topic", urgency=0
        )  # disagreement

    export = tmp_path / "out" / "eval.json"
    run_eval(
        "demo",
        db_path,
        projects_root,
        prompt_version_override=None,
        verbose=False,
        export_path=export,
        client=FakeClient([]),
        echo_fn=lambda _msg: None,
        progress_every=0,
    )
    data = json.loads(export.read_text())
    assert data["prompt_version"] == "v1"
    assert data["model"] == "claude-haiku-4-5-20251001"
    # Disagreement is captured in the export.
    assert len(data["disagreements"]) == 1
    d = data["disagreements"][0]
    assert d["human"]["category"] == "cost_complaint"
    assert d["model"]["category"] == "off_topic"
    assert "title" in d
    assert "body" in d  # field is "body", not "body_excerpt"


def test_eval_verbose_prints_disagreements(tmp_path: Path) -> None:
    projects_root = _write_project_configs(tmp_path)
    db_path = tmp_path / "data" / "demo.db"
    labels_file = labels_path("demo", projects_root=projects_root)
    labels_file.parent.mkdir(parents=True, exist_ok=True)

    with Storage(db_path) as db:
        _seed_item_and_label(
            db,
            labels_file,
            "hackernews:v1",
            title="t",
            label_category="cost_complaint",
            label_urgency=8,
        )
        _prime_classification(db, "hackernews:v1", "v1", category="off_topic", urgency=0)

    echoed: list[str] = []
    run_eval(
        "demo",
        db_path,
        projects_root,
        prompt_version_override=None,
        verbose=True,
        export_path=None,
        client=FakeClient([]),
        echo_fn=echoed.append,
        progress_every=0,
    )
    output = "\n".join(echoed)
    assert "Disagreements:" in output
    assert "hackernews:v1" in output
    assert "human=cost_complaint" in output
    assert "model=off_topic" in output


def test_eval_re_score_makes_zero_api_calls(tmp_path: Path) -> None:
    """--re-score must never instantiate the classifier."""
    projects_root = _write_project_configs(tmp_path)
    db_path = tmp_path / "data" / "demo.db"
    labels_file = labels_path("demo", projects_root=projects_root)
    labels_file.parent.mkdir(parents=True, exist_ok=True)

    with Storage(db_path) as db:
        _seed_item_and_label(
            db,
            labels_file,
            "hackernews:r1",
            title="t",
            label_category="cost_complaint",
            label_urgency=8,
        )
        _prime_classification(db, "hackernews:r1", "v1", category="cost_complaint", urgency=8)
        # Item with no classification for v1 — should be flagged missing
        # but not block the run.
        _seed_item_and_label(
            db,
            labels_file,
            "hackernews:r2",
            title="t2",
            label_category="off_topic",
            label_urgency=1,
        )

    client = FakeClient([])  # any API call would assert
    result = run_eval(
        "demo",
        db_path,
        projects_root,
        prompt_version_override=None,
        verbose=False,
        export_path=None,
        re_score=True,
        client=client,
        echo_fn=lambda _m: None,
        progress_every=0,
    )
    assert len(client.messages.calls) == 0
    assert result["run_cost"]["newly_classified"] == 0
    assert result["run_cost"]["cached"] == 1
    # The unclassified item is reported as a failure (not blocking).
    assert result["run_cost"]["failures"] == 1


def test_eval_re_score_export_contains_relabel_impact(tmp_path: Path) -> None:
    projects_root = _write_project_configs(tmp_path)
    db_path = tmp_path / "data" / "demo.db"
    labels_file = labels_path("demo", projects_root=projects_root)
    labels_file.parent.mkdir(parents=True, exist_ok=True)

    with Storage(db_path) as db:
        _seed_item_and_label(
            db,
            labels_file,
            "hackernews:rl1",
            title="t",
            label_category="self_host_intent",
            label_urgency=8,
        )
        # Simulate a relabel: append a second entry with a different
        # category + a later timestamp.
        entry = make_entry(
            item_id="hackernews:rl1",
            category="off_topic",
            urgency=0,
            note="reconsidered",
        )
        d = entry.model_dump()
        d["labeled_at"] = datetime(2099, 1, 1, tzinfo=UTC).isoformat()
        with labels_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(d) + "\n")

        _prime_classification(db, "hackernews:rl1", "v1", category="self_host_intent", urgency=8)

    export = tmp_path / "out.json"
    run_eval(
        "demo",
        db_path,
        projects_root,
        prompt_version_override=None,
        verbose=False,
        export_path=export,
        re_score=True,
        client=FakeClient([]),
        echo_fn=lambda _m: None,
        progress_every=0,
    )
    data = json.loads(export.read_text())
    assert data["mode"] == "re-score"
    ri = data["relabel_impact"]
    assert ri["total_relabeled"] == 1
    r = ri["relabels"][0]
    assert r["item_id"] == "hackernews:rl1"
    assert r["old_category"] == "self_host_intent"
    assert r["new_category"] == "off_topic"
    assert "self_host_intent → off_topic" in ri["migrations"]


def test_eval_re_score_uses_latest_label_against_cached_classification(
    tmp_path: Path,
) -> None:
    """The whole point of --re-score: measure a cached classification
    against a corrected label, and watch the accuracy shift without
    re-classifying."""
    projects_root = _write_project_configs(tmp_path)
    db_path = tmp_path / "data" / "demo.db"
    labels_file = labels_path("demo", projects_root=projects_root)
    labels_file.parent.mkdir(parents=True, exist_ok=True)

    with Storage(db_path) as db:
        # Original label was self_host_intent.
        _seed_item_and_label(
            db,
            labels_file,
            "hackernews:rl2",
            title="t",
            label_category="self_host_intent",
            label_urgency=8,
        )
        _prime_classification(db, "hackernews:rl2", "v1", category="off_topic", urgency=0)
        # Relabel: now off_topic — the classifier was right all along.
        entry = make_entry(
            item_id="hackernews:rl2",
            category="off_topic",
            urgency=0,
            note=None,
        )
        d = entry.model_dump()
        d["labeled_at"] = datetime(2099, 1, 1, tzinfo=UTC).isoformat()
        with labels_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(d) + "\n")

    result = run_eval(
        "demo",
        db_path,
        projects_root,
        prompt_version_override=None,
        verbose=False,
        export_path=None,
        re_score=True,
        client=FakeClient([]),
        echo_fn=lambda _m: None,
        progress_every=0,
    )
    # Accuracy against the NEW label: 1/1 agreement.
    assert result["metrics"]["overall_accuracy"]["accuracy"] == 1.0


def test_compute_relabel_impact_ignores_single_entry_items(tmp_path: Path) -> None:
    from social_surveyor.cli_eval import _compute_relabel_impact

    labels_file = tmp_path / "labeled.jsonl"
    append_label(
        labels_file,
        make_entry(item_id="a:1", category="cost_complaint", urgency=8, note=None),
    )
    append_label(
        labels_file,
        make_entry(item_id="b:2", category="off_topic", urgency=0, note=None),
    )
    ri = _compute_relabel_impact(labels_file)
    assert ri["total_relabeled"] == 0
    assert ri["relabels"] == []


def test_compute_relabel_impact_ignores_identical_repeats(tmp_path: Path) -> None:
    from social_surveyor.cli_eval import _compute_relabel_impact

    labels_file = tmp_path / "labeled.jsonl"
    # Two identical entries for the same item — not a real relabel,
    # just a Ctrl-C retry pattern.
    append_label(
        labels_file,
        make_entry(item_id="a:1", category="cost_complaint", urgency=8, note=None),
    )
    append_label(
        labels_file,
        make_entry(item_id="a:1", category="cost_complaint", urgency=8, note=None),
    )
    ri = _compute_relabel_impact(labels_file)
    assert ri["total_relabeled"] == 0


def test_eval_latest_wins_on_duplicate_labels(tmp_path: Path) -> None:
    projects_root = _write_project_configs(tmp_path)
    db_path = tmp_path / "data" / "demo.db"
    labels_file = labels_path("demo", projects_root=projects_root)
    labels_file.parent.mkdir(parents=True, exist_ok=True)

    with Storage(db_path) as db:
        source, platform_id = "hackernews", "dup1"
        db.upsert_item(
            RawItem(
                source=source,
                platform_id=platform_id,
                url="https://ex/",
                title="t",
                body="b",
                author=None,
                created_at=datetime(2026, 4, 1, tzinfo=UTC),
                raw_json={},
            )
        )
        # Two labels for the same item_id; later one should win.
        older = make_entry(
            item_id="hackernews:dup1",
            category="off_topic",
            urgency=0,
            note=None,
        )
        newer = make_entry(
            item_id="hackernews:dup1",
            category="cost_complaint",
            urgency=8,
            note=None,
        )
        # Force distinct timestamps so latest-wins is unambiguous.
        older_dict = older.model_dump()
        older_dict["labeled_at"] = datetime(2026, 3, 1, tzinfo=UTC).isoformat()
        labels_file.write_text(
            json.dumps(older_dict) + "\n" + newer.model_dump_json() + "\n",
            encoding="utf-8",
        )
        _prime_classification(db, "hackernews:dup1", "v1", category="cost_complaint", urgency=8)

    result = run_eval(
        "demo",
        db_path,
        projects_root,
        prompt_version_override=None,
        verbose=False,
        export_path=None,
        client=FakeClient([]),
        echo_fn=lambda _msg: None,
        progress_every=0,
    )
    # Latest label is cost_complaint and classifier matches → 1.0 accuracy.
    assert result["metrics"]["overall_accuracy"]["accuracy"] == 1.0
