from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from social_surveyor.config import GitHubQuery, GitHubSourceConfig
from social_surveyor.sources.base import SourceInitError
from social_surveyor.sources.github import (
    GitHubRateLimitError,
    GitHubSource,
    _body_matches_any_token,
    _query_tokens,
)
from social_surveyor.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures"


def _load_issues() -> dict[str, Any]:
    return json.loads((FIXTURES / "github_search_issues.json").read_text())


def _load_comments() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "github_issue_comments.json").read_text())


def _build_handler(
    *,
    search_default: dict[str, Any] | None = None,
    search_comments_scope: dict[str, Any] | None = None,
    comments_by_path: dict[str, list[dict[str, Any]]] | None = None,
    rate_limit: bool = False,
):
    """Route requests to pre-baked fixtures based on path and query params."""
    comments_by_path = comments_by_path or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if rate_limit:
            return httpx.Response(
                403,
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "9999999999"},
                json={"message": "API rate limit exceeded"},
            )
        if request.url.path == "/search/issues":
            q = request.url.params.get("q", "")
            if "in:comments" in q:
                return httpx.Response(200, json=search_comments_scope or {"items": []})
            return httpx.Response(200, json=search_default or {"items": []})
        if request.url.path.endswith("/comments"):
            return httpx.Response(200, json=comments_by_path.get(request.url.path, []))
        return httpx.Response(404, json={"message": "unexpected path"})

    return handler


def _make_source(
    tmp_path: Path,
    handler,
    *,
    cfg: GitHubSourceConfig | None = None,
) -> tuple[GitHubSource, Storage]:
    cfg = cfg or GitHubSourceConfig(queries=[GitHubQuery(q="prometheus storage expensive")])
    client = httpx.Client(transport=httpx.MockTransport(handler))
    db = Storage(tmp_path / "t.db")
    source = GitHubSource(cfg, db, client=client, token="fake-token")
    return source, db


def test_init_requires_github_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    cfg = GitHubSourceConfig(queries=[GitHubQuery(q="x")])
    with Storage(tmp_path / "t.db") as db, pytest.raises(SourceInitError) as exc:
        GitHubSource(cfg, db)
    assert "GITHUB_TOKEN" in str(exc.value)


def test_fetch_produces_issue_item_with_expected_shape(tmp_path: Path) -> None:
    issues = _load_issues()
    handler = _build_handler(search_default=issues, search_comments_scope={"items": []})
    source, db = _make_source(tmp_path, handler)
    try:
        items = source.fetch()
    finally:
        db.close()

    issue_items = [i for i in items if i.platform_id.startswith("github-issue-")]
    assert len(issue_items) == 2

    first = next(i for i in issue_items if i.platform_id == "github-issue-999001")
    assert first.source == "github"
    assert first.url == "https://github.com/prometheus/prometheus/issues/1234"
    assert first.title == "Prometheus long-term storage is expensive"
    assert first.author == "alice"
    assert first.raw_json["search_scope"] == "default"
    assert first.raw_json["matched_query"] == "prometheus storage expensive"
    assert first.raw_json["is_pr"] is False

    # The second fixture item has a pull_request field; we tag it.
    pr_item = next(i for i in issue_items if i.platform_id == "github-issue-999002")
    assert pr_item.raw_json["is_pr"] is True


def test_fetch_follows_in_comments_to_extract_matching_comments(tmp_path: Path) -> None:
    issues = _load_issues()
    comments = _load_comments()
    handler = _build_handler(
        search_default={"items": []},
        search_comments_scope={"items": [issues["items"][0]]},  # prometheus/prometheus#1234
        comments_by_path={"/repos/prometheus/prometheus/issues/1234/comments": comments},
    )
    source, db = _make_source(
        tmp_path,
        handler,
        cfg=GitHubSourceConfig(
            queries=[GitHubQuery(q="thanos costs")],
        ),
    )
    try:
        items = source.fetch()
    finally:
        db.close()

    comment_items = [i for i in items if i.platform_id.startswith("github-comment-")]
    # Tokens "thanos" and "costs" match comments 777001 (both) and
    # 777003 ("thanos"). Comment 777002 matches nothing.
    matched_ids = {i.platform_id for i in comment_items}
    assert matched_ids == {"github-comment-777001", "github-comment-777003"}

    first = next(i for i in comment_items if i.platform_id == "github-comment-777001")
    assert first.url.endswith("#issuecomment-777001")
    assert first.author == "carol"
    assert first.title.startswith("Comment on:")
    assert first.raw_json["search_scope"] == "in:comments"
    assert first.raw_json["parent_issue"]["number"] == 1234
    assert first.raw_json["parent_issue"]["html_url"].endswith("/issues/1234")


def test_fetch_advances_cursor_to_highest_created_at(tmp_path: Path) -> None:
    issues = _load_issues()
    handler = _build_handler(search_default=issues, search_comments_scope={"items": []})
    source, db = _make_source(tmp_path, handler)
    try:
        source.fetch()
        key = "issues:prometheus storage expensive"
        assert db.get_cursor("github", key) == "2026-04-15T10:00:00Z"
    finally:
        db.close()


def test_fetch_sends_created_gt_cursor_on_subsequent_poll(tmp_path: Path) -> None:
    seen_qs: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/issues":
            seen_qs.append(request.url.params.get("q", ""))
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json=[])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = GitHubSourceConfig(queries=[GitHubQuery(q="thanos")])
    with Storage(tmp_path / "t.db") as db:
        db.set_cursor("github", "issues:thanos", "2026-04-10T00:00:00Z")
        source = GitHubSource(cfg, db, client=client, token="t")
        source.fetch()

    assert all("created:>2026-04-10T00:00:00Z" in q for q in seen_qs)


def test_orgs_watchlist_logs_warning(tmp_path: Path) -> None:
    import structlog

    issues = _load_issues()
    handler = _build_handler(search_default=issues, search_comments_scope={"items": []})
    cfg = GitHubSourceConfig(
        queries=[GitHubQuery(q="x")],
        orgs_watchlist=["prometheus", "grafana"],
    )
    source, db = _make_source(tmp_path, handler, cfg=cfg)
    try:
        with structlog.testing.capture_logs() as cap:
            source.fetch()
    finally:
        db.close()

    watchlist_events = [e for e in cap if e.get("event") == "github.watchlist_match"]
    owners = {e["owner"] for e in watchlist_events}
    assert owners == {"prometheus", "grafana"}


def test_rate_limit_response_retries_then_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            403,
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "9999999999"},
            json={"message": "API rate limit exceeded"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = GitHubSourceConfig(queries=[GitHubQuery(q="x")])
    # Strip tenacity's wait so the test runs fast.
    from social_surveyor.sources import github as gh_mod

    monkeypatch.setattr(gh_mod.GitHubSource._http_get_json.retry, "wait", lambda *a, **k: 0)

    with Storage(tmp_path / "t.db") as db:
        source = GitHubSource(cfg, db, client=client, token="t")
        with pytest.raises(GitHubRateLimitError):
            source.fetch()

    assert call_count == 3  # stop_after_attempt(3)


def test_max_comment_fetches_per_poll_cap(tmp_path: Path) -> None:
    # Two issues matched in comments; cap at 1 so we only fetch comments
    # for the first one.
    issues = _load_issues()
    comments = _load_comments()
    fetches = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/issues":
            q = request.url.params.get("q", "")
            if "in:comments" in q:
                return httpx.Response(200, json=issues)
            return httpx.Response(200, json={"items": []})
        if request.url.path.endswith("/comments"):
            fetches["count"] += 1
            return httpx.Response(200, json=comments)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = GitHubSourceConfig(
        queries=[GitHubQuery(q="thanos")],
        max_comment_fetches_per_poll=1,
    )
    with Storage(tmp_path / "t.db") as db:
        source = GitHubSource(cfg, db, client=client, token="t")
        source.fetch()

    assert fetches["count"] == 1


def test_query_tokens_drops_operators_and_short_words() -> None:
    tokens = _query_tokens('"thanos OR mimir" costs prometheus')
    assert "thanos" in tokens
    assert "mimir" in tokens
    assert "costs" in tokens
    assert "prometheus" in tokens
    assert "or" not in tokens


def test_body_matches_any_token_case_insensitive() -> None:
    assert _body_matches_any_token("We switched to Thanos last week.", ["thanos"])
    assert not _body_matches_any_token("Nothing relevant here.", ["thanos", "mimir"])


def test_query_tokens_strips_org_qualifier_values() -> None:
    # Regression: without qualifier stripping, "org:prometheus" leaked
    # `org` (matching "organization", "reorg") and `prometheus` (matching
    # any comment that happens to name the project) as content tokens.
    q = "victoriametrics (org:prometheus OR org:grafana OR org:thanos-io OR org:cortexproject)"
    tokens = _query_tokens(q)
    assert tokens == ["victoriametrics"]
    assert "org" not in tokens
    assert "prometheus" not in tokens
    assert "grafana" not in tokens
    assert "thanos-io" not in tokens


def test_query_tokens_strips_in_and_is_qualifiers() -> None:
    tokens = _query_tokens("thanos in:comments -is:pr")
    assert tokens == ["thanos"]
    assert "comments" not in tokens
    assert "is" not in tokens


def test_query_tokens_strips_type_and_created_qualifiers() -> None:
    tokens = _query_tokens('"cardinality explosion" type:issue created:>2024-01-01')
    assert set(tokens) == {"cardinality", "explosion"}


def test_query_tokens_preserves_content_next_to_qualifiers() -> None:
    # Qualifier stripping is surgical — surrounding content survives.
    tokens = _query_tokens("datadog bill org:prometheus hidden costs")
    assert "datadog" in tokens
    assert "bill" in tokens
    assert "hidden" in tokens
    assert "costs" in tokens
    assert "prometheus" not in tokens  # stripped from qualifier
