from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer

from social_surveyor.cli_ingest import _detect_source, run_ingest
from social_surveyor.storage import Storage
from tests.test_classifier import FakeClient, FakeResponse, _body_after_prefill
from tests.test_cli_classify import _write_project_configs


def _project_with_routing(tmp_path: Path) -> Path:
    projects_root = _write_project_configs(tmp_path)
    (projects_root / "demo" / "routing.yaml").write_text(
        """
version: 1
immediate:
  threshold_urgency: 7
  alert_worthy_categories: [cost_complaint]
  webhook_secret: TEST_X
digest:
  schedule: {hour: 9, minute: 0, timezone: UTC}
  webhook_secret: TEST_Y
""",
        encoding="utf-8",
    )
    return projects_root


def _classify_handler(req: httpx.Request) -> httpx.Response:
    """Stand-in for Anthropic's messages.create — returns a canned JSON
    body the prompt-prefill logic expects."""
    # The Classifier uses the anthropic SDK, not a raw HTTP call, so
    # we won't actually intercept here. This handler covers the HN/
    # Reddit/X endpoints below.
    return httpx.Response(404, text="not found")


# --- _detect_source() -------------------------------------------------------


def test_detect_source_recognizes_each_supported_host() -> None:
    assert _detect_source("https://news.ycombinator.com/item?id=42") == "hackernews"
    assert _detect_source("https://www.reddit.com/r/devops/comments/abc/title/") == "reddit"
    assert _detect_source("https://old.reddit.com/r/devops/comments/abc/") == "reddit"
    assert _detect_source("https://x.com/foo/status/1234567890") == "x"
    assert _detect_source("https://twitter.com/foo/status/1234567890") == "x"


def test_detect_source_rejects_unsupported() -> None:
    with pytest.raises(typer.BadParameter, match="unsupported source"):
        _detect_source("https://www.example.com/article/1")


# --- HN ingest --------------------------------------------------------------


def _hn_algolia_response(
    *,
    id_: int = 41234567,
    title: str = "Datadog costs doubled",
    text: str = "Body text &amp; stuff.",
    author: str = "someuser",
) -> dict[str, Any]:
    return {
        "id": id_,
        "type": "story",
        "title": title,
        "author": author,
        "text": text,
        "created_at": "2026-04-19T12:00:00.000Z",
        "url": f"https://news.ycombinator.com/item?id={id_}",
    }


def test_ingest_hn_item_inserts_and_classifies(tmp_path: Path) -> None:
    projects_root = _project_with_routing(tmp_path)
    db_path = tmp_path / "data" / "demo.db"

    def handler(req: httpx.Request) -> httpx.Response:
        if "hn.algolia.com/api/v1/items/" in str(req.url):
            return httpx.Response(200, json=_hn_algolia_response())
        return httpx.Response(404)

    anthropic = FakeClient(
        [FakeResponse(_body_after_prefill(category="cost_complaint", urgency=8))]
    )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = run_ingest(
        "demo",
        db_path,
        projects_root,
        url="https://news.ycombinator.com/item?id=41234567",
        http_client=client,
        anthropic_client=anthropic,
        echo_fn=lambda _m="": None,
    )
    client.close()

    assert result["source"] == "hackernews"
    assert result["item_id"] == "hackernews:41234567"
    assert result["inserted"] is True

    with Storage(db_path) as db:
        row = db.get_item_by_id("hackernews", "41234567")
        assert row is not None
        assert "Datadog costs doubled" in row["title"]
        assert row["body"] and "Body text" in row["body"]
        clf = db.get_classification("hackernews:41234567", "v1")
        assert clf is not None
        assert clf["category"] == "cost_complaint"


# --- Reddit ingest ----------------------------------------------------------


def _reddit_comments_response(
    *,
    post_id: str = "abc123",
    title: str = "What's the best Prometheus LTS option?",
    selftext: str = "I'm evaluating Mimir vs Thanos.",
    author: str = "redditor",
    subreddit: str = "devops",
) -> list[dict[str, Any]]:
    return [
        {
            "kind": "Listing",
            "data": {
                "children": [
                    {
                        "kind": "t3",
                        "data": {
                            "id": post_id,
                            "title": title,
                            "selftext": selftext,
                            "author": author,
                            "subreddit": subreddit,
                            "permalink": f"/r/{subreddit}/comments/{post_id}/stub/",
                            "created_utc": 1713533200.0,
                        },
                    }
                ]
            },
        },
        {"kind": "Listing", "data": {"children": []}},  # comments listing
    ]


def test_ingest_reddit_post_inserts_with_t3_prefix(tmp_path: Path) -> None:
    projects_root = _project_with_routing(tmp_path)
    db_path = tmp_path / "data" / "demo.db"

    def handler(req: httpx.Request) -> httpx.Response:
        if "reddit.com/comments/" in str(req.url):
            return httpx.Response(200, json=_reddit_comments_response())
        return httpx.Response(404)

    anthropic = FakeClient(
        [FakeResponse(_body_after_prefill(category="self_host_intent", urgency=7))]
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = run_ingest(
        "demo",
        db_path,
        projects_root,
        url="https://www.reddit.com/r/devops/comments/abc123/slug/",
        http_client=client,
        anthropic_client=anthropic,
        echo_fn=lambda _m="": None,
    )
    client.close()

    assert result["source"] == "reddit"
    assert result["item_id"] == "reddit:t3_abc123"
    with Storage(db_path) as db:
        row = db.get_item_by_id("reddit", "t3_abc123")
        assert row is not None
        assert row["url"].endswith("/r/devops/comments/abc123/stub/")


def test_ingest_reddit_already_in_db_does_not_double_insert(tmp_path: Path) -> None:
    projects_root = _project_with_routing(tmp_path)
    db_path = tmp_path / "data" / "demo.db"

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_reddit_comments_response())

    from social_surveyor.types import RawItem

    # Pre-seed the item so upsert returns False.
    with Storage(db_path) as db:
        db.upsert_item(
            RawItem(
                source="reddit",
                platform_id="t3_abc123",
                url="https://reddit.com/r/devops/comments/abc123/stub/",
                title="Existing",
                body=None,
                author=None,
                created_at=datetime.now(UTC),
                raw_json={},
            )
        )
        initial_count = db.count_items()

    anthropic = FakeClient([FakeResponse(_body_after_prefill(category="off_topic", urgency=0))])
    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = run_ingest(
        "demo",
        db_path,
        projects_root,
        url="https://www.reddit.com/r/devops/comments/abc123/slug/",
        http_client=client,
        anthropic_client=anthropic,
        echo_fn=lambda _m="": None,
    )
    client.close()

    assert result["inserted"] is False
    with Storage(db_path) as db:
        assert db.count_items() == initial_count


# --- errors -----------------------------------------------------------------


def test_ingest_rejects_unsupported_url(tmp_path: Path) -> None:
    projects_root = _project_with_routing(tmp_path)
    db_path = tmp_path / "data" / "demo.db"
    with pytest.raises(typer.BadParameter, match="unsupported source"):
        run_ingest(
            "demo",
            db_path,
            projects_root,
            url="https://example.com/article/1",
            http_client=httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(404))),
            echo_fn=lambda _m="": None,
        )


def test_ingest_hn_without_id_fails(tmp_path: Path) -> None:
    projects_root = _project_with_routing(tmp_path)
    db_path = tmp_path / "data" / "demo.db"
    with pytest.raises(typer.BadParameter, match="missing"):
        run_ingest(
            "demo",
            db_path,
            projects_root,
            url="https://news.ycombinator.com/item?q=foo",
            http_client=httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(404))),
            echo_fn=lambda _m="": None,
        )
