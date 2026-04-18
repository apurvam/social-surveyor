from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from typer.testing import CliRunner

from social_surveyor.cli import app
from social_surveyor.sources.base import Source
from social_surveyor.sources.hackernews import HackerNewsSource
from social_surveyor.storage import Storage
from social_surveyor.types import RawItem

FIXTURES = Path(__file__).parent / "fixtures"

runner = CliRunner()


def _minimal_project(tmp_path: Path) -> Path:
    """Build a temporary project dir with HN configured.

    Returns the temp root; the CLI is invoked with cwd set there so
    ``projects/<n>/sources/*.yaml`` resolves correctly, and the real DB
    lands under ``data/<n>.db`` in the temp dir.
    """
    proj = tmp_path / "projects" / "demo" / "sources"
    proj.mkdir(parents=True)
    (proj / "hackernews.yaml").write_text(
        "queries: [prometheus]\ntags: [story, comment]\nmax_results_per_query: 50\n"
    )
    return tmp_path


def _hn_fixture() -> dict[str, Any]:
    return json.loads((FIXTURES / "hackernews_search.json").read_text())


def _invoke(args: list[str], cwd: Path, env: dict[str, str] | None = None):
    import os

    old_cwd = Path.cwd()
    old_env = os.environ.copy()
    try:
        os.chdir(cwd)
        if env is not None:
            os.environ.update(env)
        return runner.invoke(app, args)
    finally:
        os.chdir(old_cwd)
        os.environ.clear()
        os.environ.update(old_env)


def test_poll_single_source_writes_to_real_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _minimal_project(tmp_path)

    # Patch the HN source's HTTP client at construction time.
    original_init = HackerNewsSource.__init__

    def patched_init(self, cfg, storage, **kwargs):
        client = httpx.Client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=_hn_fixture()))
        )
        original_init(self, cfg, storage, client=client, **kwargs)

    monkeypatch.setattr(HackerNewsSource, "__init__", patched_init)

    result = _invoke(["poll", "--project", "demo", "--source", "hackernews"], cwd=root)
    assert result.exit_code == 0, result.stdout

    db_path = root / "data" / "demo.db"
    assert db_path.is_file()
    with Storage(db_path) as db:
        assert db.count_items(source="hackernews") == 3


class _FailingSource(Source):
    name = "reddit"

    def fetch(self, since_id: str | None = None) -> list[RawItem]:
        raise RuntimeError("simulated reddit failure")

    def backfill(self, days: int) -> list[RawItem]:
        return []


class _SucceedingSource(Source):
    name = "hackernews"

    def __init__(self, item_ids: list[str]) -> None:
        self._ids = item_ids

    def fetch(self, since_id: str | None = None) -> list[RawItem]:
        return [
            RawItem(
                source="hackernews",
                platform_id=i,
                url=f"https://example.com/{i}",
                title=f"item {i}",
                body=None,
                author=None,
                created_at=datetime.now(UTC),
                raw_json={},
            )
            for i in self._ids
        ]

    def backfill(self, days: int) -> list[RawItem]:
        return []


def test_poll_all_sources_continues_when_one_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If reddit blows up, hackernews still runs and its items are persisted."""
    root = tmp_path
    proj = root / "projects" / "demo" / "sources"
    proj.mkdir(parents=True)
    (proj / "reddit.yaml").write_text(
        "subreddits: [devops]\nqueries: [q]\nreddit_username: tester\n"
    )
    (proj / "hackernews.yaml").write_text("queries: [q]\n")

    # Replace the SOURCE_BUILDERS entries for reddit and hackernews with
    # our stubs so no real HTTP/PRAW calls happen.
    import social_surveyor.cli as cli_mod

    patched = dict(cli_mod.SOURCE_BUILDERS)
    patched["reddit"] = lambda *_args, **_kw: _FailingSource()
    patched["hackernews"] = lambda *_args, **_kw: _SucceedingSource(["a", "b"])
    monkeypatch.setattr(cli_mod, "SOURCE_BUILDERS", patched)

    result = _invoke(["poll", "--project", "demo"], cwd=root)
    assert result.exit_code == 0, result.stdout

    with Storage(root / "data" / "demo.db") as db:
        # Reddit rows: 0 (it failed). HN rows: 2.
        assert db.count_items(source="reddit") == 0
        assert db.count_items(source="hackernews") == 2


def test_dry_run_for_x_does_not_hit_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The X source is special: --dry-run must not make HTTP calls."""
    root = tmp_path
    proj = root / "projects" / "demo" / "sources"
    proj.mkdir(parents=True)
    (proj / "x.yaml").write_text(
        "queries:\n"
        "  - name: q1\n"
        "    query: 'test'\n"
        "max_results_per_query: 100\n"
        "daily_read_cap: 500\n"
    )

    forbidden_client = MagicMock(spec=httpx.Client)
    forbidden_client.get.side_effect = AssertionError("X dry-run must not make HTTP calls")

    import social_surveyor.cli as cli_mod
    from social_surveyor.sources.x import XSource

    def build_x(cfg, storage):
        return XSource(cfg.x, storage, client=forbidden_client, bearer_token="fake-token")

    patched = dict(cli_mod.SOURCE_BUILDERS)
    patched["x"] = build_x
    monkeypatch.setattr(cli_mod, "SOURCE_BUILDERS", patched)

    result = _invoke(["poll", "--project", "demo", "--source", "x", "--dry-run"], cwd=root)
    assert result.exit_code == 0, result.stdout
    assert forbidden_client.get.called is False
    # dry_run_state payload is printed to stdout as JSON.
    assert '"dry_run_state"' in result.stdout


def test_usage_command_reports_zero_when_no_prior_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path
    proj = root / "projects" / "demo" / "sources"
    proj.mkdir(parents=True)
    (proj / "x.yaml").write_text("queries:\n  - name: q1\n    query: 'test'\ndaily_read_cap: 500\n")
    # usage requires the DB to exist — create it empty by opening once.
    (root / "data").mkdir()
    Storage(root / "data" / "demo.db").close()

    result = _invoke(["usage", "--project", "demo"], cwd=root)
    assert result.exit_code == 0, result.stdout
    report = json.loads(result.stdout.splitlines()[-1])
    assert report["x"]["tier"] == "pay-per-use"
    assert report["x"]["used_today"] == 0
    assert report["x"]["used_this_month"] == 0
    assert report["x"]["daily_read_cap"] == 500


def test_usage_command_errors_cleanly_when_db_missing(tmp_path: Path) -> None:
    root = tmp_path
    proj = root / "projects" / "demo" / "sources"
    proj.mkdir(parents=True)
    (proj / "x.yaml").write_text("queries:\n  - name: q1\n    query: 'test'\n")

    result = _invoke(["usage", "--project", "demo"], cwd=root)
    assert result.exit_code == 1
    assert "no DB" in (result.stdout + (result.stderr or ""))
