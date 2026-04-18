from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from social_surveyor.cli_triage import (
    Decision,
    _parse_group_key,
    _suggested_yaml_changes,
    run_triage,
)
from social_surveyor.storage import Storage
from social_surveyor.types import RawItem


def _seed(tmp_path: Path, groups: dict[str, int]) -> Path:
    """Seed a DB with N items per group_key; returns the db path."""
    db_path = tmp_path / "data" / "demo.db"
    now = datetime(2026, 4, 17, 12, 0, tzinfo=UTC)
    with Storage(db_path) as db:
        n = 0
        for group, count in groups.items():
            source, _, _ = group.partition(":")
            for _ in range(count):
                n += 1
                db.upsert_item(
                    RawItem(
                        source=source,
                        platform_id=str(n),
                        url=f"https://example.com/{n}",
                        title=f"Item {n} in {group}",
                        body=f"Body of item {n}",
                        author=f"user{n}",
                        created_at=now,
                        raw_json={"group_key": group},
                    )
                )
    return db_path


class _Script:
    def __init__(self, inputs: list[str]) -> None:
        self.inputs = list(inputs)
        self.echoed: list[str] = []

    def input(self, _prompt: str = "") -> str:
        return self.inputs.pop(0)

    def echo(self, text: str = "") -> None:
        self.echoed.append(text)


def test_parse_group_key_reddit_splits_subreddit_and_query() -> None:
    s, sub, q = _parse_group_key("reddit:r/devops/prometheus storage")
    assert s == "reddit"
    assert sub == "devops"
    assert q == "prometheus storage"


def test_parse_group_key_non_reddit_passes_through() -> None:
    s, sub, q = _parse_group_key("hackernews:datadog cost")
    assert s == "hackernews"
    assert sub is None
    assert q == "datadog cost"


def test_triage_writes_report_with_decisions(tmp_path: Path) -> None:
    db_path = _seed(
        tmp_path,
        {
            "reddit:r/devops/prometheus storage": 10,
            "hackernews:datadog cost": 20,
        },
    )
    # Groups come out count-DESC: hackernews (20) first, reddit (10) second.
    script = _Script(["d", "k"])  # drop hackernews, keep reddit

    now = datetime(2026, 4, 17, 14, 30, tzinfo=UTC)
    report = run_triage(
        "demo",
        db_path,
        tmp_path,
        source_filter=None,
        limit=5,
        input_fn=script.input,
        echo_fn=script.echo,
        now=now,
    )

    assert report.exists()
    text = report.read_text()
    assert "reddit:r/devops/prometheus storage" in text
    assert "KEEP" in text
    assert "hackernews:datadog cost" in text
    assert "DROP" in text
    # Hackernews was DROP, so its YAML should have suggested changes.
    assert "projects/<project>/sources/hackernews.yaml" in text


def test_triage_quit_produces_aborted_report(tmp_path: Path) -> None:
    db_path = _seed(
        tmp_path,
        {
            "hackernews:q1": 5,
            "hackernews:q2": 5,
        },
    )
    script = _Script(["q"])

    report = run_triage(
        "demo",
        db_path,
        tmp_path,
        source_filter=None,
        limit=5,
        input_fn=script.input,
        echo_fn=script.echo,
    )

    text = report.read_text()
    assert "quit before every group was reviewed" in text


def test_triage_view_more_advances_offset(tmp_path: Path) -> None:
    db_path = _seed(tmp_path, {"hackernews:q1": 15})
    # View more, then keep.
    script = _Script(["v", "k"])

    report = run_triage(
        "demo",
        db_path,
        tmp_path,
        source_filter=None,
        limit=5,
        input_fn=script.input,
        echo_fn=script.echo,
    )

    # Should have echoed two group renders (initial + view-more page).
    renders = [msg for msg in script.echoed if "=== hackernews:q1 ===" in msg]
    assert len(renders) == 2
    # And the decision should be KEEP.
    assert "KEEP" in report.read_text()


def test_triage_unknown_input_reprompts(tmp_path: Path) -> None:
    db_path = _seed(tmp_path, {"hackernews:q1": 3})
    script = _Script(["xxx", "k"])  # junk, then keep

    report = run_triage(
        "demo",
        db_path,
        tmp_path,
        source_filter=None,
        limit=5,
        input_fn=script.input,
        echo_fn=script.echo,
    )

    echoed_text = "\n".join(script.echoed)
    assert "unknown choice" in echoed_text
    assert "KEEP" in report.read_text()


def test_triage_source_filter_restricts_groups(tmp_path: Path) -> None:
    db_path = _seed(
        tmp_path,
        {
            "reddit:r/devops/q": 5,
            "hackernews:q": 5,
        },
    )
    # Only hackernews should be prompted.
    script = _Script(["k"])
    report = run_triage(
        "demo",
        db_path,
        tmp_path,
        source_filter="hackernews",
        limit=5,
        input_fn=script.input,
        echo_fn=script.echo,
    )
    text = report.read_text()
    assert "hackernews:q" in text
    assert "reddit:" not in text


def test_suggested_yaml_changes_reddit_drop_summary() -> None:
    decisions = [
        Decision(
            group_key="reddit:r/homelab/datadog",
            decision="drop",
            item_count=3,
        ),
        Decision(
            group_key="reddit:r/homelab/observability",
            decision="drop",
            item_count=2,
        ),
    ]
    lines = _suggested_yaml_changes(decisions)
    text = "\n".join(lines)
    assert "reddit.yaml" in text
    assert "r/homelab" in text
    assert "homelab" in text  # flagged subreddit surfaced


def test_triage_errors_cleanly_when_db_missing(tmp_path: Path) -> None:
    import pytest
    import typer

    missing = tmp_path / "data" / "nope.db"
    with pytest.raises(typer.BadParameter):
        run_triage("demo", missing, tmp_path, source_filter=None, limit=5)
