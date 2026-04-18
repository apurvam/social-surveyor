from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from social_surveyor.config import HackerNewsSourceConfig
from social_surveyor.sources.hackernews import HackerNewsSource, _strip_html
from social_surveyor.storage import Storage

FIXTURE = Path(__file__).parent / "fixtures" / "hackernews_search.json"


def _load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def _mock_client(responses: list[dict[str, Any]]) -> httpx.Client:
    """Build an httpx.Client that returns the given JSON payloads in order."""
    it = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/search_by_date"
        return httpx.Response(200, json=next(it))

    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport, base_url="https://hn.algolia.com")


def test_fetch_produces_story_and_comment_items(tmp_path: Path) -> None:
    fixture = _load_fixture()
    client = _mock_client([fixture])
    cfg = HackerNewsSourceConfig(queries=["prometheus storage"])
    with Storage(tmp_path / "t.db") as db:
        source = HackerNewsSource(cfg, db, client=client)
        items = source.fetch()

    assert len(items) == 3
    by_id = {i.platform_id: i for i in items}

    # Story with external URL
    story = by_id["41234567"]
    assert story.source == "hackernews"
    assert story.url == "https://example.com/blog/prom-storage"
    assert story.title == "Prometheus long-term storage is expensive"
    assert story.body is None
    assert story.author == "alice"

    # Ask-HN story with no external url: URL falls back to HN item page
    ask_hn = by_id["41234890"]
    assert ask_hn.url == "https://news.ycombinator.com/item?id=41234890"
    assert ask_hn.body == "Looking at Thanos vs Mimir. Datadog is killing our budget."

    # Comment synthesizes a title and uses the HN item URL
    comment = by_id["41235000"]
    assert comment.title.startswith("Comment by carol on HN")
    assert comment.url == "https://news.ycombinator.com/item?id=41235000"
    assert comment.body == "We moved off Datadog to self-hosted Prometheus + Thanos. Saved 80%."


def test_fetch_advances_cursor_to_highest_created_at_i(tmp_path: Path) -> None:
    fixture = _load_fixture()
    client = _mock_client([fixture])
    cfg = HackerNewsSourceConfig(queries=["prometheus storage"])

    with Storage(tmp_path / "t.db") as db:
        source = HackerNewsSource(cfg, db, client=client)
        source.fetch()
        cursor = db.get_cursor("hackernews", "prometheus storage")

    # Highest created_at_i in the fixture is 1776232800
    assert cursor == "1776232800"


def test_fetch_sends_since_cursor_on_subsequent_poll(tmp_path: Path) -> None:
    sent_params: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_params.append(dict(request.url.params))
        return httpx.Response(200, json={"hits": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = HackerNewsSourceConfig(queries=["q1"])

    with Storage(tmp_path / "t.db") as db:
        db.set_cursor("hackernews", "q1", "1700000000")
        source = HackerNewsSource(cfg, db, client=client)
        source.fetch()

    assert sent_params[0]["numericFilters"] == "created_at_i>1700000000"


def test_fetch_filters_by_configured_tags(tmp_path: Path) -> None:
    # With tags=["story"] only, the comment should be dropped even if
    # Algolia returned it.
    fixture = _load_fixture()
    client = _mock_client([fixture])
    cfg = HackerNewsSourceConfig(queries=["q"], tags=["story"])

    with Storage(tmp_path / "t.db") as db:
        source = HackerNewsSource(cfg, db, client=client)
        items = source.fetch()

    assert all("comment" not in set(i.raw_json.get("_tags", [])) for i in items)
    assert {i.platform_id for i in items} == {"41234567", "41234890"}


def test_backfill_sends_cutoff_timestamp_filter(tmp_path: Path) -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(dict(request.url.params))
        return httpx.Response(200, json={"hits": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = HackerNewsSourceConfig(queries=["q"])
    days = 3
    expected_min = int((datetime.now(UTC) - timedelta(days=days)).timestamp()) - 5

    with Storage(tmp_path / "t.db") as db:
        source = HackerNewsSource(cfg, db, client=client)
        source.backfill(days=days)

    numeric_filter = sent[0]["numericFilters"]
    assert numeric_filter.startswith("created_at_i>")
    ts = int(numeric_filter.split(">")[1])
    assert ts >= expected_min


def test_http_error_is_retried_and_reraised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Strip tenacity's sleep so the test runs instantly.
    from social_surveyor.sources import hackernews as hn_mod

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    # Replace the retry's wait with an immediate one.
    monkeypatch.setattr(
        hn_mod.HackerNewsSource._get_hits.retry,
        "wait",
        lambda *a, **k: 0,
    )
    cfg = HackerNewsSourceConfig(queries=["q"])

    with Storage(tmp_path / "t.db") as db:
        source = HackerNewsSource(cfg, db, client=client)
        with pytest.raises(httpx.HTTPStatusError):
            source.fetch()

    assert call_count == 4  # stop_after_attempt(4) in the source


def test_strip_html_unescapes_entities_and_drops_tags() -> None:
    # Real shape observed on live Algolia in Phase B: comment_text
    # carries both HTML entities and inline <p>/<a> tags.
    raw = (
        "You&#x27;re right that OBI is structural.<p>On the gRPC-as-client-layer "
        'point, see <a href="https://example.com">this</a>.</p>'
    )
    cleaned = _strip_html(raw)
    assert "&#x27;" not in cleaned
    assert "<" not in cleaned
    assert "You're right" in cleaned
    assert "see this." in cleaned


def test_fetch_sends_typo_tolerance_and_advanced_syntax(tmp_path: Path) -> None:
    """Regression: without typoTolerance=false, Algolia returned e.g.
    `catalog`-containing comments for `query=datadog`. advancedSyntax=true
    enables literal-phrase matching via double-quotes in the value.
    Both must appear on every outgoing Algolia request."""
    sent_params: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_params.append(dict(request.url.params))
        return httpx.Response(200, json={"hits": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = HackerNewsSourceConfig(queries=["datadog"])
    with Storage(tmp_path / "t.db") as db:
        HackerNewsSource(cfg, db, client=client).fetch()

    assert sent_params, "no requests were made"
    for p in sent_params:
        assert p.get("typoTolerance") == "false", p
        assert p.get("advancedSyntax") == "true", p


def test_backfill_sends_typo_tolerance_and_advanced_syntax(tmp_path: Path) -> None:
    sent_params: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_params.append(dict(request.url.params))
        return httpx.Response(200, json={"hits": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = HackerNewsSourceConfig(queries=["q"])
    with Storage(tmp_path / "t.db") as db:
        HackerNewsSource(cfg, db, client=client).backfill(days=3)

    assert sent_params
    for p in sent_params:
        assert p.get("typoTolerance") == "false", p
        assert p.get("advancedSyntax") == "true", p


def test_fetch_cleans_comment_body_html(tmp_path: Path) -> None:
    payload = {
        "hits": [
            {
                "objectID": "99",
                "created_at": "2026-04-17T10:00:00.000Z",
                "created_at_i": 1776232800,
                "author": "alice",
                "story_id": 42,
                "comment_text": (
                    "We&#x27;re paying Datadog &amp; it&#x27;s expensive.<p>Considering Thanos.</p>"
                ),
                "_tags": ["comment", "author_alice", "story_42"],
            }
        ]
    }
    client = _mock_client([payload])
    with Storage(tmp_path / "t.db") as db:
        items = HackerNewsSource(HackerNewsSourceConfig(queries=["x"]), db, client=client).fetch()
    body = items[0].body or ""
    assert "&#x27;" not in body
    assert "&amp;" not in body
    assert "<p>" not in body
    assert "We're paying Datadog & it's expensive." in body
