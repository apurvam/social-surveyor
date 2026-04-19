from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from social_surveyor.cli_classify import run_classify
from social_surveyor.storage import Storage
from social_surveyor.types import RawItem
from tests.test_classifier import FakeClient, FakeResponse, _body_after_prefill


def _write_project_configs(tmp_path: Path) -> Path:
    """Build a minimal projects/demo/ tree with a classifier.yaml that
    points at the opendata categories."""
    root = tmp_path / "projects"
    demo = root / "demo"
    (demo / "sources").mkdir(parents=True)
    (demo / "sources" / "reddit.yaml").write_text(
        """
subreddits: [devops]
queries: ["prometheus"]
reddit_username: user
""",
        encoding="utf-8",
    )
    (demo / "categories.yaml").write_text(
        """
version: 1
categories:
  - id: cost_complaint
    label: Cost
    description: Cost pain.
  - id: self_host_intent
    label: Self
    description: Self.
  - id: competitor_pain
    label: Comp
    description: Comp.
  - id: off_topic
    label: "Off"
    description: "Off."
urgency_scale:
  - range: [0, 5]
    meaning: low
  - range: [6, 10]
    meaning: high
""",
        encoding="utf-8",
    )
    (demo / "classifier.yaml").write_text(
        """
version: 1
prompt_version: "v1"
icp_description: "A test ICP."
few_shot_examples: []
model: "claude-haiku-4-5-20251001"
max_tokens: 500
temperature: 0.0
max_retries: 1
backoff_seconds: 0.0
""",
        encoding="utf-8",
    )
    return root


def _seed_items(db: Storage, count: int = 3) -> list[str]:
    ids: list[str] = []
    for i in range(count):
        item_id = f"hackernews:hn{i}"
        db.upsert_item(
            RawItem(
                source="hackernews",
                platform_id=f"hn{i}",
                url=f"https://hn/{i}",
                title=f"Title {i}",
                body=f"Body text {i}",
                author="alice",
                created_at=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
                raw_json={"id": f"hn{i}"},
            )
        )
        ids.append(item_id)
    return ids


def test_classify_one_item_by_id(tmp_path: Path) -> None:
    projects_root = _write_project_configs(tmp_path)
    db_path = tmp_path / "data" / "demo.db"
    with Storage(db_path) as db:
        _seed_items(db, 1)

    echoed: list[str] = []
    client = FakeClient([FakeResponse(_body_after_prefill(urgency=8))])

    result = run_classify(
        "demo",
        db_path,
        projects_root,
        item_id="hackernews:hn0",
        limit=None,
        prompt_version_override=None,
        dry_run=False,
        client=client,
        echo_fn=echoed.append,
    )

    assert result == {"classified": 1, "failed": 0, "dry_run": 0}
    # Classification was persisted under v1.
    with Storage(db_path) as db:
        c = db.get_classification("hackernews:hn0", "v1")
        assert c is not None
        assert c["category"] == "cost_complaint"


def test_classify_batch_skips_items_already_classified_for_this_version(
    tmp_path: Path,
) -> None:
    """Re-running classify must be a no-op on warm cache — that's what
    makes the eval loop fast."""
    projects_root = _write_project_configs(tmp_path)
    db_path = tmp_path / "data" / "demo.db"
    with Storage(db_path) as db:
        _seed_items(db, 3)

    echoed: list[str] = []
    first_responses = [FakeResponse(_body_after_prefill()) for _ in range(3)]
    first_client = FakeClient(first_responses)
    run_classify(
        "demo",
        db_path,
        projects_root,
        item_id=None,
        limit=None,
        prompt_version_override=None,
        dry_run=False,
        client=first_client,
        echo_fn=echoed.append,
    )
    assert len(first_client.messages.calls) == 3

    # Second run: no responses queued. If the cache is honored we shouldn't
    # hit the client at all.
    second_client = FakeClient([])
    result = run_classify(
        "demo",
        db_path,
        projects_root,
        item_id=None,
        limit=None,
        prompt_version_override=None,
        dry_run=False,
        client=second_client,
        echo_fn=echoed.append,
    )
    assert result["classified"] == 0
    assert len(second_client.messages.calls) == 0


def test_classify_prompt_version_override_stamps_override(tmp_path: Path) -> None:
    projects_root = _write_project_configs(tmp_path)
    db_path = tmp_path / "data" / "demo.db"
    with Storage(db_path) as db:
        _seed_items(db, 1)

    client = FakeClient([FakeResponse(_body_after_prefill())])
    run_classify(
        "demo",
        db_path,
        projects_root,
        item_id="hackernews:hn0",
        limit=None,
        prompt_version_override="v2-experimental",
        dry_run=False,
        client=client,
        echo_fn=lambda _msg: None,
    )
    with Storage(db_path) as db:
        # Under v2-experimental only.
        assert db.get_classification("hackernews:hn0", "v1") is None
        c = db.get_classification("hackernews:hn0", "v2-experimental")
        assert c is not None
        assert c["prompt_version"] == "v2-experimental"


def test_classify_dry_run_does_not_call_api(tmp_path: Path) -> None:
    projects_root = _write_project_configs(tmp_path)
    db_path = tmp_path / "data" / "demo.db"
    with Storage(db_path) as db:
        _seed_items(db, 2)

    echoed: list[str] = []
    client = FakeClient([])  # zero responses — a real call would raise
    result = run_classify(
        "demo",
        db_path,
        projects_root,
        item_id=None,
        limit=None,
        prompt_version_override=None,
        dry_run=True,
        client=client,
        echo_fn=echoed.append,
    )
    assert result == {"classified": 0, "failed": 0, "dry_run": 2}
    assert len(client.messages.calls) == 0
    # The dry-run output includes the system-prompt marker.
    assert any("=== SYSTEM ===" in line for line in echoed)


def test_classify_limit_caps_items(tmp_path: Path) -> None:
    projects_root = _write_project_configs(tmp_path)
    db_path = tmp_path / "data" / "demo.db"
    with Storage(db_path) as db:
        _seed_items(db, 5)

    client = FakeClient([FakeResponse(_body_after_prefill()) for _ in range(2)])
    result = run_classify(
        "demo",
        db_path,
        projects_root,
        item_id=None,
        limit=2,
        prompt_version_override=None,
        dry_run=False,
        client=client,
        echo_fn=lambda _msg: None,
    )
    assert result["classified"] == 2
    assert len(client.messages.calls) == 2


def test_classify_unknown_item_raises_bad_parameter(tmp_path: Path) -> None:
    import typer

    projects_root = _write_project_configs(tmp_path)
    db_path = tmp_path / "data" / "demo.db"
    with Storage(db_path) as db:
        _seed_items(db, 1)

    with pytest.raises(typer.BadParameter):
        run_classify(
            "demo",
            db_path,
            projects_root,
            item_id="hackernews:does_not_exist",
            limit=None,
            prompt_version_override=None,
            dry_run=False,
            client=FakeClient([]),
            echo_fn=lambda _msg: None,
        )
