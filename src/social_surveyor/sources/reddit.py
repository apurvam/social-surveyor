"""RSS-based Reddit source.

Reddit closed self-service API access in November 2025 under the
Responsible Builder Policy. RSS feeds remain publicly available with
no authentication, so we poll those instead. The dormant PRAW-based
implementation is preserved in :mod:`.reddit_api` for the day an
approval comes through.

Trade-offs vs PRAW:
- No comment coverage. RSS gives posts only.
- Shallow backfill. The search RSS endpoint typically returns 25-100
  most-recent items; :meth:`backfill` logs a warning when the window
  is narrower than requested.
- No native incremental cursor. ``since_id`` is accepted for API parity
  but ignored; the storage layer's ``(source, platform_id)`` unique
  constraint handles dedup across polls.
"""

from __future__ import annotations

import html
import time
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from urllib.parse import urlencode

import feedparser
import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from ..config import RedditSourceConfig
from ..types import RawItem
from .base import Source

log = structlog.get_logger(__name__)

SEARCH_URL_TEMPLATE = "https://www.reddit.com/r/{subreddit}/search.rss"
NEW_URL_TEMPLATE = "https://www.reddit.com/r/{subreddit}/new.rss"

# Hard ceiling on how long we'll sleep for a rate-limit reset. A
# reasonable reddit reset is ~5-10 minutes; anything longer suggests
# something unusual (e.g., account-level throttling) — better to fail
# than to hang a whole poll cycle.
_MAX_RATELIMIT_SLEEP = 900.0


def _parse_rate_limit_reset(headers) -> float | None:  # type: ignore[no-untyped-def]
    """Parse reddit's x-ratelimit-reset header into seconds.

    Reddit returns fractional seconds as a string (e.g. ``"314"``).
    Returns None if the header is missing or unparseable.
    """
    raw = headers.get("x-ratelimit-reset")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class RedditForbiddenError(httpx.HTTPError):
    """Reddit returned 403. Usually a User-Agent problem; do not retry.

    We catch this as a fatal error for one poll cycle rather than
    looping and making the User-Agent problem worse.
    """


def _is_retryable(exc: BaseException) -> bool:
    """Retry on transient HTTP errors; never retry on 403 (User-Agent issue)."""
    if isinstance(exc, RedditForbiddenError):
        return False
    return isinstance(exc, httpx.HTTPError)


class RedditSource(Source):
    """Reddit poller backed by per-subreddit search RSS.

    A single :class:`RedditSource` instance holds one shared
    :class:`httpx.Client` and one ``last_request_monotonic`` timestamp
    used to throttle consecutive requests to at least
    ``cfg.min_seconds_between_requests`` apart.
    """

    name = "reddit"

    def __init__(
        self,
        cfg: RedditSourceConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.cfg = cfg
        self._client = client if client is not None else httpx.Client(timeout=30.0)
        self._user_agent = _build_user_agent(cfg.reddit_username)
        self._last_request_monotonic: float | None = None

    # ------------------------------------------------------------------ fetch

    def fetch(self, since_id: str | None = None) -> list[RawItem]:
        """Poll every configured (subreddit, query) pair.

        ``since_id`` is accepted for API parity with other sources but
        ignored — Reddit's RSS endpoint has no meaningful cursor.
        Dedup is handled downstream by ``Storage.upsert_item``.
        """
        items: list[RawItem] = []
        for subreddit in self.cfg.subreddits:
            for query in self.cfg.queries:
                feed_items = self._fetch_search(subreddit, query, time_filter=self.cfg.time_filter)
                log.info(
                    "reddit.fetch",
                    subreddit=subreddit,
                    query=query,
                    results=len(feed_items),
                )
                items.extend(feed_items)
        return items

    # --------------------------------------------------------------- backfill

    def backfill(self, days: int) -> list[RawItem]:
        """Best-effort backfill against a narrow RSS window.

        Pulls both per-query search feeds (to catch matching posts) and
        ``/r/<sub>/new.rss`` (to catch recent posts that didn't match a
        query — the classifier will judge relevance). Filters by
        ``created_at >= now - days`` client-side and warns when the
        oldest item returned is newer than the requested window.
        """
        cutoff = datetime.now(UTC) - timedelta(days=days)
        items: list[RawItem] = []
        fetched_count = 0
        oldest_item: datetime | None = None

        for subreddit in self.cfg.subreddits:
            per_sub: list[RawItem] = []

            for query in self.cfg.queries:
                per_sub.extend(
                    self._fetch_search(subreddit, query, time_filter=_days_to_time_filter(days))
                )
            per_sub.extend(self._fetch_new(subreddit))

            fetched_count += len(per_sub)
            for item in per_sub:
                if oldest_item is None or item.created_at < oldest_item:
                    oldest_item = item.created_at

            in_window = [i for i in per_sub if i.created_at >= cutoff]
            items.extend(in_window)

            if per_sub:
                oldest_in_sub = min(i.created_at for i in per_sub)
                if oldest_in_sub > cutoff:
                    # RSS returned a window narrower than the caller asked
                    # for. Warn so operators know they can't treat this as
                    # a deep backfill.
                    log.warning(
                        "backfill.window_narrower_than_requested",
                        source=self.name,
                        subreddit=subreddit,
                        days_requested=days,
                        oldest_item_age_hours=(datetime.now(UTC) - oldest_in_sub).total_seconds()
                        / 3600,
                    )

        log.info(
            "backfill.complete",
            source=self.name,
            fetched_count=fetched_count,
            after_client_filter_count=len(items),
            days_requested=days,
        )
        return items

    # -------------------------------------------------------- HTTP + parsing

    def _fetch_search(self, subreddit: str, query: str, *, time_filter: str) -> list[RawItem]:
        params = {
            "q": query,
            "sort": "new",
            "restrict_sr": 1,  # load-bearing; without it Reddit searches all of Reddit
            "t": time_filter,
            "limit": self.cfg.limit_per_query,
        }
        url = SEARCH_URL_TEMPLATE.format(subreddit=subreddit) + "?" + urlencode(params)
        return self._fetch_url(url, subreddit=subreddit, group_key=f"reddit:r/{subreddit}/{query}")

    def _fetch_new(self, subreddit: str) -> list[RawItem]:
        url = (
            NEW_URL_TEMPLATE.format(subreddit=subreddit)
            + "?"
            + urlencode({"limit": self.cfg.limit_per_query})
        )
        return self._fetch_url(url, subreddit=subreddit, group_key=f"reddit:r/{subreddit}/(new)")

    def _fetch_url(self, url: str, *, subreddit: str, group_key: str) -> list[RawItem]:
        body = self._get_with_retry(url)
        if not _looks_like_feed(body):
            # Reddit sometimes serves an HTML rate-limit or error page
            # with a 200 status. feedparser is lenient and won't flag
            # that as bozo, so we sniff the body ourselves.
            log.warning(
                "reddit.feed.unparseable",
                url=url,
                reason="response body is not XML/Atom",
                body_prefix=body[:80].decode("utf-8", errors="replace"),
            )
            return []
        parsed = feedparser.parse(body)
        if parsed.bozo and parsed.entries == []:
            log.warning(
                "reddit.feed.unparseable",
                url=url,
                reason=str(parsed.bozo_exception) if parsed.bozo_exception else "unknown",
            )
            return []
        return [
            _entry_to_raw_item(e, subreddit=subreddit, group_key=group_key) for e in parsed.entries
        ]

    @retry(
        # Explicitly exclude RedditForbiddenError — a 403 indicates a
        # User-Agent issue that retries won't fix.
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    def _get_with_retry(self, url: str) -> bytes:
        self._throttle()
        resp = self._client.get(url, headers={"User-Agent": self._user_agent})
        self._last_request_monotonic = time.monotonic()
        if resp.status_code == 403:
            raise RedditForbiddenError(
                f"Reddit returned 403 for {url} — likely a User-Agent issue; "
                f"check reddit_username in the config."
            )
        if resp.status_code == 429:
            # Reddit returns x-ratelimit-reset as seconds until the
            # bucket refills. Sleep that long (plus a small buffer)
            # before letting tenacity retry; otherwise exponential
            # backoff's 30s ceiling is too short to recover from a
            # ~5-minute reset.
            sleep_seconds = _parse_rate_limit_reset(resp.headers)
            if sleep_seconds is not None and sleep_seconds <= _MAX_RATELIMIT_SLEEP:
                log.warning(
                    "reddit.rate_limited.backing_off",
                    url=url,
                    sleep_seconds=sleep_seconds,
                    ratelimit_used=resp.headers.get("x-ratelimit-used"),
                    ratelimit_remaining=resp.headers.get("x-ratelimit-remaining"),
                )
                time.sleep(sleep_seconds + 1.0)
            else:
                log.warning(
                    "reddit.rate_limited.unknown_reset",
                    url=url,
                    headers={
                        k: v for k, v in resp.headers.items() if k.lower().startswith("x-ratelimit")
                    },
                )
        resp.raise_for_status()
        return resp.content

    def _throttle(self) -> None:
        if self._last_request_monotonic is None:
            return
        elapsed = time.monotonic() - self._last_request_monotonic
        wait = self.cfg.min_seconds_between_requests - elapsed
        if wait > 0:
            time.sleep(wait)


# --------------------------------------------------------------- module-level


def _looks_like_feed(body: bytes) -> bool:
    """Sniff whether the response body is an XML/Atom feed.

    feedparser is lenient enough that an HTML rate-limit page doesn't
    trigger ``bozo``; we need to check ourselves.
    """
    head = body[:200].lstrip().lower()
    return head.startswith((b"<?xml", b"<feed", b"<rss"))


def _build_user_agent(reddit_username: str) -> str:
    """Polite User-Agent per Reddit's API rules: include a package name
    and contact info."""
    try:
        pkg_version = version("social-surveyor")
    except PackageNotFoundError:  # pragma: no cover
        pkg_version = "0.0.0"
    return f"social-surveyor/{pkg_version} (by /u/{reddit_username})"


def _days_to_time_filter(days: int) -> str:
    if days <= 1:
        return "day"
    if days <= 7:
        return "week"
    if days <= 31:
        return "month"
    if days <= 366:
        return "year"
    return "all"


class _TagStripper(HTMLParser):
    """Minimal HTML→text converter for Reddit's summary field."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)

    def get_text(self) -> str:
        return "".join(self._chunks).strip()


def _strip_html(s: str) -> str:
    parser = _TagStripper()
    parser.feed(s)
    parser.close()
    return parser.get_text()


def _entry_author(entry: Any) -> str | None:
    """Reddit's RSS encodes the author as ``/u/<name>``.

    feedparser exposes this as either ``entry.author`` (a string) or
    ``entry.authors[0].name`` depending on the feed shape; try both.
    Strip the ``/u/`` prefix for consistency with the PRAW-era shape.
    """
    raw = None
    author_val = entry.get("author") if hasattr(entry, "get") else None
    if author_val:
        raw = author_val
    else:
        authors = entry.get("authors") if hasattr(entry, "get") else None
        if authors:
            raw = authors[0].get("name") if isinstance(authors[0], dict) else None
    if not raw:
        return None
    if raw.startswith("/u/"):
        return raw[3:]
    if raw.startswith("u/"):
        return raw[2:]
    return raw


def _entry_created_at(entry: Any) -> datetime:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        # Last-ditch: use now() so the item gets stored but flagged in
        # logs. Better than dropping an otherwise-valid entry.
        log.debug("reddit.entry.missing_timestamp", id=entry.get("id"))
        return datetime.now(UTC)
    return datetime(*parsed[:6], tzinfo=UTC)


def _entry_to_raw_item(entry: Any, *, subreddit: str, group_key: str) -> RawItem:
    raw_summary = entry.get("summary", "") or ""
    body = _strip_html(raw_summary) if raw_summary else None
    title = html.unescape(entry.get("title", "") or "")
    platform_id = str(entry.get("id") or entry.get("link") or "")
    return RawItem(
        source="reddit",
        platform_id=platform_id,
        url=entry.get("link", ""),
        title=title,
        body=body or None,
        author=_entry_author(entry),
        created_at=_entry_created_at(entry),
        raw_json={
            **_entry_to_dict(entry),
            "subreddit": subreddit,
            "group_key": group_key,
        },
    )


def _entry_to_dict(entry: Any) -> dict[str, Any]:
    """feedparser's FeedParserDict doesn't always serialize cleanly;
    coerce known-nested pieces to plain types."""
    out: dict[str, Any] = {}
    for k in entry:
        v = entry[k]
        try:
            # Round-trip through a json-safe primitive conversion. Most
            # top-level fields are already primitives; struct_time needs
            # care.
            if hasattr(v, "tm_year"):
                out[k] = list(v)
            elif isinstance(v, list | tuple):
                out[k] = [dict(x) if hasattr(x, "keys") else x for x in v]
            elif hasattr(v, "keys"):
                out[k] = dict(v)
            else:
                out[k] = v
        except Exception:  # pragma: no cover
            out[k] = str(v)
    return out
