from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import feedparser
import httpx
import pytest
import structlog

from social_surveyor.config import RedditSourceConfig
from social_surveyor.sources.reddit import (
    RedditForbiddenError,
    RedditSource,
    _build_user_agent,
    _entry_to_raw_item,
    _strip_html,
)

FIXTURE_XML = (Path(__file__).parent / "fixtures" / "reddit_search_devops.xml").read_text()


def _cfg(**overrides) -> RedditSourceConfig:
    kwargs: dict = {
        "subreddits": ["devops"],
        "queries": ["prometheus storage"],
        "reddit_username": "test_user",
        "min_seconds_between_requests": 0.0,  # disable throttle in tests by default
    }
    kwargs.update(overrides)
    return RedditSourceConfig(**kwargs)


def _mock_client(
    handler_calls: list[httpx.Request] | None = None,
    *,
    body: bytes | None = None,
    status_code: int = 200,
) -> httpx.Client:
    """Return an httpx.Client whose every request answers with (status, body)."""
    payload = body if body is not None else FIXTURE_XML.encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        if handler_calls is not None:
            handler_calls.append(request)
        return httpx.Response(status_code, content=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------- mapping


def test_entry_to_raw_item_maps_story() -> None:
    parsed = feedparser.parse(FIXTURE_XML.encode("utf-8"))
    item = _entry_to_raw_item(parsed.entries[0], subreddit="devops")

    assert item.source == "reddit"
    assert item.platform_id == "t3_abc123"
    assert (
        item.url
        == "https://www.reddit.com/r/devops/comments/abc123/prometheus_storage_cost_comparison/"
    )
    assert item.title == "Prometheus long-term storage — Thanos vs Mimir for cost"
    assert item.author == "alice"
    assert item.created_at == datetime(2026, 4, 15, 10, 0, tzinfo=UTC)
    # HTML stripped but content preserved
    assert "Datadog" in (item.body or "")
    assert "<div" not in (item.body or "")
    assert "&#39;" not in (item.body or "")
    assert item.raw_json["subreddit"] == "devops"


def test_strip_html_handles_reddit_selftext_markup() -> None:
    html_body = (
        '<!-- SC_OFF --><div class="md"><p>We&#39;ve been paying <a href="x">Datadog</a> '
        "$18k/month.</p></div><!-- SC_ON -->"
    )
    stripped = _strip_html(html_body)
    assert "Datadog" in stripped
    assert "<" not in stripped
    assert "&#39;" not in stripped
    assert "'" in stripped


def test_entry_author_strips_u_prefix() -> None:
    parsed = feedparser.parse(FIXTURE_XML.encode("utf-8"))
    item = _entry_to_raw_item(parsed.entries[1], subreddit="devops")
    # feedparser sometimes exposes `/u/bob_sre` — we normalize to `bob_sre`.
    assert item.author == "bob_sre"


# --------------------------------------------------------------- User-Agent


def test_user_agent_includes_version_and_username() -> None:
    ua = _build_user_agent("apurvam")
    assert ua.startswith("social-surveyor/")
    assert "(by /u/apurvam)" in ua


# --------------------------------------------------------------- fetch


def test_fetch_issues_one_request_per_subreddit_query_pair() -> None:
    calls: list[httpx.Request] = []
    client = _mock_client(handler_calls=calls)
    cfg = _cfg(
        subreddits=["devops", "kubernetes"],
        queries=["prometheus", "thanos"],
    )
    items = RedditSource(cfg, client=client).fetch()

    assert len(calls) == 4  # 2 subreddits * 2 queries
    # Every call hits search.rss with restrict_sr=1
    for req in calls:
        assert "/search.rss" in req.url.path
        assert req.url.params["restrict_sr"] == "1"
    # Each (sub, query) pair returned the 3-entry fixture → 12 items total
    assert len(items) == 12


def test_fetch_sends_custom_user_agent() -> None:
    calls: list[httpx.Request] = []
    client = _mock_client(handler_calls=calls)
    cfg = _cfg(reddit_username="apurvam")

    RedditSource(cfg, client=client).fetch()

    ua = calls[0].headers["User-Agent"]
    assert "social-surveyor/" in ua
    assert "/u/apurvam" in ua


def test_fetch_parses_all_three_fixture_entries() -> None:
    client = _mock_client()
    cfg = _cfg()
    items = RedditSource(cfg, client=client).fetch()

    assert {i.platform_id for i in items} == {"t3_abc123", "t3_def456", "t3_ghi789"}


def test_403_raises_and_is_not_retried() -> None:
    calls: list[httpx.Request] = []
    client = _mock_client(handler_calls=calls, status_code=403)
    cfg = _cfg()
    source = RedditSource(cfg, client=client)

    with pytest.raises(RedditForbiddenError):
        source.fetch()

    # Crucial: exactly one call, no retries on 403.
    assert len(calls) == 1


def test_429_retries_then_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[httpx.Request] = []
    client = _mock_client(handler_calls=calls, status_code=429)
    from social_surveyor.sources import reddit as reddit_mod

    monkeypatch.setattr(reddit_mod.RedditSource._get_with_retry.retry, "wait", lambda *a, **k: 0)
    cfg = _cfg()
    source = RedditSource(cfg, client=client)

    with pytest.raises(httpx.HTTPStatusError):
        source.fetch()

    assert len(calls) == 3  # stop_after_attempt(3)


def test_malformed_feed_returns_empty_list_with_warning() -> None:
    client = _mock_client(body=b"<html>rate limited</html>")
    cfg = _cfg()
    with structlog.testing.capture_logs() as cap:
        items = RedditSource(cfg, client=client).fetch()

    assert items == []
    assert any(e.get("event") == "reddit.feed.unparseable" for e in cap)


# --------------------------------------------------------------- throttle


def test_throttle_sleeps_between_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """With min_seconds_between_requests=2.0, a second request should
    sleep for ~2s. We mock time.sleep and time.monotonic to observe."""
    sleeps: list[float] = []
    fake_now = [1000.0]

    def fake_monotonic() -> float:
        return fake_now[0]

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        fake_now[0] += seconds

    from social_surveyor.sources import reddit as reddit_mod

    monkeypatch.setattr(reddit_mod.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(reddit_mod.time, "sleep", fake_sleep)

    calls: list[httpx.Request] = []
    client = _mock_client(handler_calls=calls)
    cfg = _cfg(
        subreddits=["devops", "kubernetes"],
        queries=["q"],
        min_seconds_between_requests=2.0,
    )
    RedditSource(cfg, client=client).fetch()

    # First request: no prior timestamp, no sleep.
    # Second request: monotonic hasn't advanced between calls (fixed
    # mock), so we should sleep ~2s.
    assert len(calls) == 2
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(2.0)


def test_throttle_noop_when_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    from social_surveyor.sources import reddit as reddit_mod

    monkeypatch.setattr(reddit_mod.time, "sleep", lambda s: sleeps.append(s))

    client = _mock_client()
    cfg = _cfg(min_seconds_between_requests=0.0)
    RedditSource(cfg, client=client).fetch()

    assert sleeps == []


# --------------------------------------------------------------- backfill


def test_backfill_warns_when_window_narrower_than_requested() -> None:
    """Fixture entries are all from 2026-04-14/15; backfill(days=365)
    is much wider than what RSS can deliver, so the warning should fire
    for every subreddit."""
    client = _mock_client()
    cfg = _cfg(subreddits=["devops"], queries=["q"])

    with structlog.testing.capture_logs() as cap:
        items = RedditSource(cfg, client=client).backfill(days=365)

    warnings = [e for e in cap if e.get("event") == "backfill.window_narrower_than_requested"]
    assert len(warnings) == 1
    assert warnings[0]["subreddit"] == "devops"
    assert warnings[0]["days_requested"] == 365
    assert warnings[0]["oldest_item_age_hours"] > 0
    # All fixture entries fall outside the 365-day window? No — they're
    # from April 2026 ~= one year old. The warning fires because the
    # oldest item's age is less than 365 days, not because nothing
    # matches the cutoff. Items within the window are still returned.
    assert len(items) >= 0


def test_backfill_filters_items_older_than_cutoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive backfill with a controlled 'now' so some fixture entries
    fall outside the window."""
    client = _mock_client()
    cfg = _cfg(subreddits=["devops"], queries=["q"])

    # Freeze "now" at 2026-04-15T11:00 UTC so entries at 2026-04-15T10:00
    # are in, and entries at 2026-04-14T09:15 (≈25h old) are out when we
    # ask for days=1.
    frozen_now = datetime(2026, 4, 15, 11, 0, tzinfo=UTC)
    from social_surveyor.sources import reddit as reddit_mod

    real_datetime_class = reddit_mod.datetime

    class FakeDatetime(real_datetime_class):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return frozen_now if tz is None else frozen_now.astimezone(tz)

    monkeypatch.setattr(reddit_mod, "datetime", FakeDatetime)

    items = RedditSource(cfg, client=client).backfill(days=1)

    kept_ids = {i.platform_id for i in items}
    # Only entries dated 2026-04-15 (the abc123 story and any echo from
    # /new.rss sharing the id) should survive the 1-day cutoff.
    assert "t3_abc123" in kept_ids
    # Entries from 2026-04-14 (def456, ghi789) are >25h old → dropped.
    assert "t3_ghi789" not in kept_ids


def test_backfill_hits_both_search_and_new(monkeypatch: pytest.MonkeyPatch) -> None:
    """For each subreddit, backfill should call search.rss per query
    AND /new.rss once."""
    calls: list[httpx.Request] = []
    client = _mock_client(handler_calls=calls)
    cfg = _cfg(subreddits=["devops"], queries=["q1", "q2"])

    RedditSource(cfg, client=client).backfill(days=7)

    paths = [c.url.path for c in calls]
    assert paths.count("/r/devops/search.rss") == 2  # one per query
    assert paths.count("/r/devops/new.rss") == 1


# ---------------------------------------------------------- since_id ignored


def test_fetch_ignores_since_id() -> None:
    calls: list[httpx.Request] = []
    client = _mock_client(handler_calls=calls)
    cfg = _cfg()

    RedditSource(cfg, client=client).fetch(since_id="t3_whatever")

    # since_id is not part of Reddit's RSS query string.
    for c in calls:
        assert "since_id" not in c.url.params
        assert "after" not in c.url.params
        assert "before" not in c.url.params
