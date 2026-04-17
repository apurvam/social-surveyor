from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import praw
import prawcore
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import RedditSourceConfig
from ..types import RawItem
from .base import Source, SourceInitError

if TYPE_CHECKING:
    from praw.models import Submission

log = structlog.get_logger(__name__)


# Reddit's search API only accepts coarse time filters. We map the
# user-requested ``days`` to the narrowest filter that still covers the
# window, then re-filter client-side to the exact cutoff.
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


class RedditSource(Source):
    """Reddit source backed by PRAW.

    OAuth credentials come from env vars (``REDDIT_CLIENT_ID``,
    ``REDDIT_CLIENT_SECRET``, ``REDDIT_USER_AGENT``). We use the read-only
    application-only OAuth flow — no user account, no refresh token —
    which is sufficient for searching public subreddits.

    ``since_id`` is accepted for API parity with other sources but is
    currently ignored: Reddit's search endpoint doesn't accept a
    ``before``/``after`` cursor that's meaningful in combination with
    sort=new, so we rely on storage-level dedupe.
    """

    name = "reddit"

    _REQUIRED_ENV = ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT")

    def __init__(
        self,
        cfg: RedditSourceConfig,
        *,
        client: praw.Reddit | None = None,
    ) -> None:
        self.cfg = cfg
        self._client = client if client is not None else self._build_client()

    @classmethod
    def _build_client(cls) -> praw.Reddit:
        missing = [k for k in cls._REQUIRED_ENV if not os.environ.get(k)]
        if missing:
            raise SourceInitError(
                f"Reddit source requires env vars: {', '.join(missing)}. "
                "Set them in .env or your shell."
            )
        return praw.Reddit(
            client_id=os.environ["REDDIT_CLIENT_ID"],
            client_secret=os.environ["REDDIT_CLIENT_SECRET"],
            user_agent=os.environ["REDDIT_USER_AGENT"],
            check_for_updates=False,
        )

    def fetch(self, since_id: str | None = None) -> list[RawItem]:
        items: list[RawItem] = []
        for subreddit in self.cfg.subreddits:
            for query in self.cfg.queries:
                submissions = self._search(
                    subreddit,
                    query,
                    sort="new",
                    time_filter=self.cfg.time_filter,
                    limit=self.cfg.limit_per_query,
                )
                log.info(
                    "reddit.fetch",
                    subreddit=subreddit,
                    query=query,
                    results=len(submissions),
                )
                items.extend(self._to_raw_item(s) for s in submissions)
        return items

    def backfill(self, days: int) -> list[RawItem]:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        time_filter = _days_to_time_filter(days)
        items: list[RawItem] = []
        fetched_count = 0
        for subreddit in self.cfg.subreddits:
            for query in self.cfg.queries:
                submissions = self._search(
                    subreddit,
                    query,
                    sort="new",
                    time_filter=time_filter,
                    limit=self.cfg.limit_per_query,
                )
                filtered = [
                    s
                    for s in submissions
                    if datetime.fromtimestamp(s.created_utc, tz=UTC) >= cutoff
                ]
                fetched_count += len(submissions)
                log.debug(
                    "reddit.backfill.search",
                    subreddit=subreddit,
                    query=query,
                    time_filter=time_filter,
                    results=len(submissions),
                    kept=len(filtered),
                )
                items.extend(self._to_raw_item(s) for s in filtered)
        # Diagnostic: if fetched_count >> len(items), Reddit's coarse
        # time_filter is over-fetching and we may want to tune queries.
        log.info(
            "backfill.complete",
            source="reddit",
            fetched_count=fetched_count,
            after_client_filter_count=len(items),
            days_requested=days,
            reddit_time_filter_used=time_filter,
        )
        return items

    # PRAW handles 429s internally but can still raise on transient network
    # or server errors; wrap each search call (not the whole fetch) so one
    # failed subreddit doesn't wipe out the rest of the poll.
    @retry(
        retry=retry_if_exception_type(
            (prawcore.exceptions.RequestException, prawcore.exceptions.ServerError)
        ),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    def _search(
        self,
        subreddit: str,
        query: str,
        *,
        sort: str,
        time_filter: str,
        limit: int,
    ) -> list[Submission]:
        sub = self._client.subreddit(subreddit)
        return list(sub.search(query, sort=sort, time_filter=time_filter, limit=limit))

    @staticmethod
    def _to_raw_item(submission: Submission) -> RawItem:
        author_name = submission.author.name if submission.author is not None else None
        body = submission.selftext if getattr(submission, "is_self", False) else None
        return RawItem(
            source="reddit",
            platform_id=submission.id,
            url=f"https://reddit.com{submission.permalink}",
            title=submission.title,
            body=body or None,
            author=author_name,
            created_at=datetime.fromtimestamp(submission.created_utc, tz=UTC),
            raw_json=_serialize_submission(submission),
        )


def _serialize_submission(submission: Submission) -> dict[str, Any]:
    """Capture the PRAW fields we care about without triggering extra HTTP.

    PRAW lazy-loads attributes; sticking to the ones already present in
    the listing response keeps backfill fast.
    """
    subreddit_name = getattr(submission.subreddit, "display_name", None)
    return {
        "id": submission.id,
        "title": submission.title,
        "selftext": getattr(submission, "selftext", ""),
        "subreddit": subreddit_name,
        "author": str(submission.author) if submission.author is not None else None,
        "score": getattr(submission, "score", None),
        "num_comments": getattr(submission, "num_comments", None),
        "created_utc": submission.created_utc,
        "permalink": submission.permalink,
        "url": getattr(submission, "url", None),
        "is_self": getattr(submission, "is_self", None),
        "over_18": getattr(submission, "over_18", None),
    }
