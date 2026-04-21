from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from social_surveyor.config import XQuery, XSourceConfig
from social_surveyor.sources.base import SourceInitError
from social_surveyor.sources.x import (
    BACKFILL_MAX_DAYS,
    XSource,
    XUsage,
    _synthesize_tweet_title,
    fetch_x_usage,
)
from social_surveyor.storage import Storage

FIXTURE = Path(__file__).parent / "fixtures" / "x_recent_search.json"


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def _make_handler(responses: list[dict[str, Any]], seen: list[dict[str, str]] | None = None):
    it = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(dict(request.url.params))
        assert request.url.path == "/2/tweets/search/recent"
        return httpx.Response(200, json=next(it))

    return handler


def _make_source(
    tmp_path: Path,
    handler,
    *,
    cfg: XSourceConfig | None = None,
) -> tuple[XSource, Storage]:
    cfg = cfg or XSourceConfig(
        queries=[XQuery(name="q1", query='"datadog" -is:retweet')],
        max_results_per_query=100,
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    db = Storage(tmp_path / "t.db")
    return XSource(cfg, db, client=client, bearer_token="fake-token"), db


def test_init_requires_bearer_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    cfg = XSourceConfig(queries=[XQuery(name="q", query="q")])
    with Storage(tmp_path / "t.db") as db, pytest.raises(SourceInitError) as exc:
        XSource(cfg, db)
    assert "X_BEARER_TOKEN" in str(exc.value)


def test_fetch_maps_tweets_and_joins_author_expansion(tmp_path: Path) -> None:
    handler = _make_handler([_fixture()])
    source, db = _make_source(tmp_path, handler)
    try:
        items = source.fetch()
    finally:
        db.close()

    assert len(items) == 2
    by_id = {i.platform_id: i for i in items}
    first = by_id["1900000000000000001"]
    assert first.author == "alice_ops"
    assert first.url == "https://x.com/alice_ops/status/1900000000000000001"
    assert first.source == "x"
    assert first.body.startswith("Datadog bill hit $18k")
    assert first.title.startswith("Datadog bill hit $18k")
    assert first.raw_json["query_name"] == "q1"
    assert first.raw_json["author"]["verified"] is False


def test_fetch_advances_cursor_to_newest_id(tmp_path: Path) -> None:
    handler = _make_handler([_fixture()])
    source, db = _make_source(tmp_path, handler)
    try:
        source.fetch()
        assert db.get_cursor("x", "q1") == "1900000000000000001"
    finally:
        db.close()


def test_fetch_passes_since_id_on_second_poll(tmp_path: Path) -> None:
    seen: list[dict[str, str]] = []
    handler = _make_handler([_fixture(), {"data": [], "meta": {"result_count": 0}}], seen=seen)
    source, db = _make_source(tmp_path, handler)
    try:
        source.fetch()
        source.fetch()
    finally:
        db.close()

    assert "since_id" not in seen[0]
    assert seen[1].get("since_id") == "1900000000000000001"


def test_fetch_records_api_usage(tmp_path: Path) -> None:
    handler = _make_handler([_fixture()])
    source, db = _make_source(tmp_path, handler)
    try:
        source.fetch()
        start_of_day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        assert db.sum_api_usage("x", start_of_day) == 2
        assert db.api_usage_by_query("x", start_of_day) == {"q1": 2}
    finally:
        db.close()


def test_backfill_clamps_to_seven_days(tmp_path: Path) -> None:
    seen: list[dict[str, str]] = []
    handler = _make_handler([_fixture()], seen=seen)
    source, db = _make_source(tmp_path, handler)
    try:
        source.backfill(days=30)
    finally:
        db.close()

    # start_time should be ~ 7 days ago, not 30
    start_time = seen[0]["start_time"]
    parsed = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
    delta_days = (datetime.now(UTC) - parsed).days
    assert delta_days == BACKFILL_MAX_DAYS - 1 or delta_days == BACKFILL_MAX_DAYS


def test_backfill_honors_requested_days_when_under_cap(tmp_path: Path) -> None:
    seen: list[dict[str, str]] = []
    handler = _make_handler([_fixture()], seen=seen)
    source, db = _make_source(tmp_path, handler)
    try:
        source.backfill(days=3)
    finally:
        db.close()

    parsed = datetime.fromisoformat(seen[0]["start_time"].replace("Z", "+00:00"))
    delta_days = (datetime.now(UTC) - parsed).days
    assert delta_days == 2 or delta_days == 3


def test_dry_run_state_makes_no_http_calls(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("dry_run_state must not hit the API")

    source, db = _make_source(tmp_path, handler)
    try:
        db.set_cursor("x", "q1", "1800000000000000000")
        state = source.dry_run_state()
    finally:
        db.close()

    assert state["queries"][0]["since_id"] == "1800000000000000000"
    assert state["daily_read_cap"] == source.cfg.daily_read_cap
    assert state["used_today"] == 0


def test_synthesize_tweet_title_truncates_long_text() -> None:
    long = "a" * 200
    assert _synthesize_tweet_title(long).endswith("…")
    assert len(_synthesize_tweet_title(long)) <= 80


# --- fetch_x_usage ---------------------------------------------------------


def _usage_handler(payload: dict[str, Any], *, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/2/usage/tweets"
        assert request.headers.get("Authorization") == "Bearer TEST_TOKEN"
        return httpx.Response(status, json=payload)

    return handler


def test_fetch_x_usage_parses_happy_path() -> None:
    payload = {
        "data": {
            "project_usage": 143,
            "project_cap": 10_000,
            "cap_reset_day": 21,
        }
    }
    client = httpx.Client(transport=httpx.MockTransport(_usage_handler(payload)))
    try:
        usage = fetch_x_usage("TEST_TOKEN", client=client)
    finally:
        client.close()
    assert usage == XUsage(project_usage=143, project_cap=10_000, cap_reset_day=21)
    assert usage.percent == pytest.approx(1.43)


def test_fetch_x_usage_returns_none_on_non_200() -> None:
    """Auth blip / 401 / 5xx should degrade, not raise — the digest post
    must continue."""
    client = httpx.Client(
        transport=httpx.MockTransport(_usage_handler({"detail": "auth failed"}, status=401))
    )
    try:
        assert fetch_x_usage("TEST_TOKEN", client=client) is None
    finally:
        client.close()


def test_fetch_x_usage_returns_none_on_transport_error() -> None:
    def raising(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns down")

    client = httpx.Client(transport=httpx.MockTransport(raising))
    try:
        assert fetch_x_usage("TEST_TOKEN", client=client) is None
    finally:
        client.close()


def test_fetch_x_usage_returns_none_on_missing_fields() -> None:
    """Malformed response (missing required field) shouldn't crash the
    digest footer."""
    payload = {"data": {"project_usage": 143}}  # missing project_cap, cap_reset_day
    client = httpx.Client(transport=httpx.MockTransport(_usage_handler(payload)))
    try:
        assert fetch_x_usage("TEST_TOKEN", client=client) is None
    finally:
        client.close()
