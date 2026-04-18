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
    # Header now includes a [N/M] progress prefix, so match on the
    # group_key portion rather than the literal "=== hackernews:q1 ===".
    renders = [msg for msg in script.echoed if "hackernews:q1 ===" in msg]
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


def test_suggested_yaml_changes_skips_unknown_bucket() -> None:
    """DROP on the `(unknown query)` bucket must not produce a bogus
    YAML file path like `sources/(unknown query).yaml`.
    """
    decisions = [
        Decision(group_key="(unknown query)", decision="drop", item_count=788),
    ]
    lines = _suggested_yaml_changes(decisions)
    # Only the (unknown query) bucket was dropped; since it has no
    # source YAML, the function should skip rendering a suggestion
    # section at all.
    assert lines == []


def test_suggested_yaml_changes_mixes_real_and_unknown_correctly() -> None:
    """Real DROPs still render; unknown bucket is filtered out."""
    decisions = [
        Decision(group_key="(unknown query)", decision="drop", item_count=788),
        Decision(group_key="hackernews:datadog cost", decision="drop", item_count=20),
    ]
    text = "\n".join(_suggested_yaml_changes(decisions))
    assert "hackernews.yaml" in text
    assert "(unknown query).yaml" not in text
    assert "sources/(" not in text


def test_session_complete_message_fires_when_all_groups_decided(tmp_path: Path) -> None:
    db_path = _seed(tmp_path, {"hackernews:q1": 3, "hackernews:q2": 3})
    script = _Script(["k", "k"])  # keep both, let loop finish naturally

    run_triage(
        "demo",
        db_path,
        tmp_path,
        source_filter=None,
        limit=5,
        input_fn=script.input,
        echo_fn=script.echo,
    )

    echoed = "\n".join(script.echoed)
    assert "session complete" in echoed
    assert "2 decision(s)" in echoed


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


def test_triage_digit_expands_item_full_body(tmp_path: Path) -> None:
    """Typing an item index expands that item to full body, then re-prompts."""
    # Seed a single group with one item that has a long multi-paragraph body.
    long_body = "First paragraph.\n\nSecond paragraph with more detail.\n\nThird."
    now = datetime(2026, 4, 17, 12, 0, tzinfo=UTC)
    db_path = tmp_path / "data" / "demo.db"
    with Storage(db_path) as db:
        db.upsert_item(
            RawItem(
                source="hackernews",
                platform_id="99",
                url="https://example.com/99",
                title="Long post title",
                body=long_body,
                author="alice",
                created_at=now,
                raw_json={"group_key": "hackernews:q1"},
            )
        )

    # Expand item 1, then keep.
    script = _Script(["1", "k"])
    run_triage(
        "demo",
        db_path,
        tmp_path,
        source_filter=None,
        limit=5,
        preview_chars=20,  # deliberately tight to prove expand shows more
        input_fn=script.input,
        echo_fn=script.echo,
    )

    echoed = "\n".join(script.echoed)
    # The preview truncated after 20 chars (ends with an ellipsis), so the
    # full body only appears via the expand path.
    assert "-- expanded --" in echoed
    assert "First paragraph." in echoed
    assert "Second paragraph with more detail." in echoed
    assert "Third." in echoed


def test_triage_expand_does_not_re_render_group(tmp_path: Path) -> None:
    """Regression: an earlier loop structure re-rendered the group after
    every `continue`, which meant expand flooded the screen with a fresh
    group listing on top of the expanded item."""
    db_path = _seed(tmp_path, {"hackernews:q1": 3})
    # Expand items 1, 2, 3 in sequence, then keep.
    script = _Script(["1", "2", "3", "k"])

    run_triage(
        "demo",
        db_path,
        tmp_path,
        source_filter=None,
        limit=5,
        input_fn=script.input,
        echo_fn=script.echo,
    )

    group_renders = [m for m in script.echoed if "hackernews:q1 ===" in m]
    # Only the initial render. Three expands + one decision should not
    # trigger additional group renders.
    assert len(group_renders) == 1
    expansions = [m for m in script.echoed if "-- expanded --" in m]
    assert len(expansions) == 3


def test_triage_collapse_rerenders_group_listing(tmp_path: Path) -> None:
    """`c` re-emits the group listing so the operator can re-orient after
    one or more item expansions have scrolled past."""
    db_path = _seed(tmp_path, {"hackernews:q1": 3})
    # Expand item 1, then collapse, then keep.
    script = _Script(["1", "c", "k"])

    run_triage(
        "demo",
        db_path,
        tmp_path,
        source_filter=None,
        limit=5,
        input_fn=script.input,
        echo_fn=script.echo,
    )

    group_renders = [m for m in script.echoed if "hackernews:q1 ===" in m]
    # Initial render + re-render after `c` = 2.
    assert len(group_renders) == 2


def test_triage_digit_out_of_range_is_handled(tmp_path: Path) -> None:
    db_path = _seed(tmp_path, {"hackernews:q1": 2})
    # Item 99 doesn't exist; should re-prompt, then keep.
    script = _Script(["99", "k"])
    run_triage(
        "demo",
        db_path,
        tmp_path,
        source_filter=None,
        limit=5,
        input_fn=script.input,
        echo_fn=script.echo,
    )
    echoed = "\n".join(script.echoed)
    assert "out of range" in echoed


def test_triage_preview_chars_controls_body_truncation(tmp_path: Path) -> None:
    """--preview-chars 10 shows ~10 chars; --preview-chars 500 shows more."""
    long_body = "x" * 400
    now = datetime(2026, 4, 17, 12, 0, tzinfo=UTC)
    db_path = tmp_path / "data" / "demo.db"
    with Storage(db_path) as db:
        db.upsert_item(
            RawItem(
                source="hackernews",
                platform_id="1",
                url="https://example.com/1",
                title="t",
                body=long_body,
                author="a",
                created_at=now,
                raw_json={"group_key": "hackernews:q1"},
            )
        )

    short_script = _Script(["k"])
    run_triage(
        "demo",
        db_path,
        tmp_path,
        source_filter=None,
        limit=5,
        preview_chars=10,
        input_fn=short_script.input,
        echo_fn=short_script.echo,
    )
    short_out = "\n".join(short_script.echoed)
    # With 10-char preview, the body render shows ~10 x's then an ellipsis.
    # Count runs of x's to confirm the preview respected the cap.
    assert "xxxxxxxxxx…" in short_out  # 10 x's + ellipsis
    assert "xxxxxxxxxxx" not in short_out  # never 11+ x's in a run

    long_script = _Script(["k"])
    long_db = tmp_path / "data" / "demo_long.db"
    with Storage(long_db) as db:
        db.upsert_item(
            RawItem(
                source="hackernews",
                platform_id="1",
                url="https://example.com/1",
                title="t",
                body=long_body,
                author="a",
                created_at=now,
                raw_json={"group_key": "hackernews:q1"},
            )
        )
    run_triage(
        "demo",
        long_db,
        tmp_path,
        source_filter=None,
        limit=5,
        preview_chars=500,
        input_fn=long_script.input,
        echo_fn=long_script.echo,
    )
    long_out = "\n".join(long_script.echoed)
    # With 500 chars, the whole 400-char body fits, no truncation marker.
    assert "x" * 400 in long_out
