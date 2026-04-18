from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from social_surveyor.config import RedditSourceConfig
from social_surveyor.sources.base import SourceInitError
from social_surveyor.sources.reddit_api import RedditSource, _days_to_time_filter


def _submission(
    id_: str = "abc123",
    title: str = "Prometheus storage question",
    selftext: str = "We need long-term storage. Datadog is too expensive.",
    subreddit: str = "devops",
    author: str | None = "alice",
    created_utc: float = 1_735_000_000.0,  # 2024-12-24 UTC, fixed for assertions
    permalink: str = "/r/devops/comments/abc123/question/",
    score: int = 42,
    num_comments: int = 5,
    is_self: bool = True,
    url: str = "https://reddit.com/r/devops/comments/abc123/question/",
) -> SimpleNamespace:
    author_obj: SimpleNamespace | None
    if author is None:
        author_obj = None
    else:
        author_obj = SimpleNamespace(name=author)
        # str(author_obj) needs to produce the username (PRAW's Redditor
        # stringifies to the username).
        author_obj.__str__ = lambda self=author_obj: author  # type: ignore[assignment]
    return SimpleNamespace(
        id=id_,
        title=title,
        selftext=selftext,
        subreddit=SimpleNamespace(display_name=subreddit),
        author=author_obj,
        created_utc=created_utc,
        permalink=permalink,
        score=score,
        num_comments=num_comments,
        is_self=is_self,
        url=url,
        over_18=False,
    )


def _client_returning(subs_by_query: dict[tuple[str, str], list[SimpleNamespace]]) -> MagicMock:
    """Build a mock PRAW client where ``subreddit(s).search(q, ...)`` returns the
    list registered for ``(s, q)``."""
    client = MagicMock()

    def subreddit(name: str) -> MagicMock:
        sub = MagicMock()

        def search(query: str, *_, **__) -> list[SimpleNamespace]:
            return subs_by_query.get((name, query), [])

        sub.search.side_effect = search
        return sub

    client.subreddit.side_effect = subreddit
    return client


def test_fetch_maps_submissions_to_raw_items() -> None:
    cfg = RedditSourceConfig(subreddits=["devops"], queries=["prometheus storage"])
    sub = _submission()
    client = _client_returning({("devops", "prometheus storage"): [sub]})

    source = RedditSource(cfg, client=client)
    items = source.fetch()

    assert len(items) == 1
    item = items[0]
    assert item.source == "reddit"
    assert item.platform_id == "abc123"
    assert item.url == "https://reddit.com/r/devops/comments/abc123/question/"
    assert item.title == "Prometheus storage question"
    assert item.body == "We need long-term storage. Datadog is too expensive."
    assert item.author == "alice"
    assert item.created_at == datetime.fromtimestamp(1_735_000_000.0, tz=UTC)
    assert item.raw_json["subreddit"] == "devops"
    assert item.raw_json["score"] == 42
    assert item.raw_json["num_comments"] == 5


def test_fetch_iterates_all_subreddit_query_pairs() -> None:
    cfg = RedditSourceConfig(
        subreddits=["devops", "kubernetes"],
        queries=["prometheus", "thanos"],
    )
    client = _client_returning(
        {
            ("devops", "prometheus"): [_submission("a"), _submission("b")],
            ("devops", "thanos"): [_submission("c")],
            ("kubernetes", "prometheus"): [],
            ("kubernetes", "thanos"): [_submission("d")],
        }
    )

    source = RedditSource(cfg, client=client)
    items = source.fetch()

    assert {i.platform_id for i in items} == {"a", "b", "c", "d"}
    assert client.subreddit.call_count == 4


def test_fetch_link_post_has_no_body() -> None:
    cfg = RedditSourceConfig(subreddits=["devops"], queries=["q"])
    link_post = _submission(is_self=False, selftext="")
    client = _client_returning({("devops", "q"): [link_post]})

    source = RedditSource(cfg, client=client)
    items = source.fetch()

    assert items[0].body is None


def test_fetch_handles_deleted_author() -> None:
    cfg = RedditSourceConfig(subreddits=["devops"], queries=["q"])
    sub = _submission(author=None)
    client = _client_returning({("devops", "q"): [sub]})

    source = RedditSource(cfg, client=client)
    items = source.fetch()

    assert items[0].author is None
    assert items[0].raw_json["author"] is None


def test_backfill_filters_items_older_than_cutoff() -> None:
    cfg = RedditSourceConfig(subreddits=["devops"], queries=["q"])
    now = datetime.now(UTC)
    recent = _submission("recent", created_utc=(now - timedelta(days=3)).timestamp())
    old = _submission("old", created_utc=(now - timedelta(days=20)).timestamp())
    client = _client_returning({("devops", "q"): [recent, old]})

    source = RedditSource(cfg, client=client)
    items = source.backfill(days=7)

    assert [i.platform_id for i in items] == ["recent"]


def test_backfill_uses_search_time_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = RedditSourceConfig(subreddits=["devops"], queries=["q"])
    seen: dict[str, object] = {}

    def fake_search(self, subreddit, query, *, sort, time_filter, limit):
        seen["subreddit"] = subreddit
        seen["query"] = query
        seen["sort"] = sort
        seen["time_filter"] = time_filter
        return []

    monkeypatch.setattr(RedditSource, "_search", fake_search)
    source = RedditSource(cfg, client=MagicMock())
    source.backfill(days=7)

    assert seen["time_filter"] == "week"
    assert seen["sort"] == "new"


def test_days_to_time_filter_buckets() -> None:
    assert _days_to_time_filter(1) == "day"
    assert _days_to_time_filter(7) == "week"
    assert _days_to_time_filter(30) == "month"
    assert _days_to_time_filter(90) == "year"
    assert _days_to_time_filter(400) == "all"


def test_init_raises_source_init_error_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT"):
        monkeypatch.delenv(var, raising=False)
    cfg = RedditSourceConfig(subreddits=["devops"], queries=["q"])
    with pytest.raises(SourceInitError) as exc:
        RedditSource(cfg)
    assert "REDDIT_CLIENT_ID" in str(exc.value)
