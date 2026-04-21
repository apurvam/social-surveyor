from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import XQuery, XSourceConfig
from ..storage import Storage
from ..types import RawItem
from .base import Source, SourceInitError

log = structlog.get_logger(__name__)

API_BASE = "https://api.x.com/2"
RECENT_SEARCH_PATH = "/tweets/search/recent"
USAGE_TWEETS_PATH = "/usage/tweets"

# Recent Search has a hard 7-day retention window. Anything older needs
# full-archive search, which has different pricing and isn't wired up
# here yet — see the session 2 PR description.
BACKFILL_MAX_DAYS = 7


@dataclass(frozen=True)
class XUsage:
    """Authoritative project-level X usage as returned by ``GET /2/usage/tweets``.

    Fields match the X API response shape. X doesn't publish a dollar
    figure via the API — the ``Total Cost`` on the Developer Console is
    rendered client-side only — so this dataclass captures consumption
    only. Callers render "N of M this month" rather than a $ estimate.
    """

    project_usage: int
    project_cap: int
    cap_reset_day: int

    @property
    def percent(self) -> float:
        return (self.project_usage / self.project_cap * 100.0) if self.project_cap > 0 else 0.0


def fetch_x_usage(
    bearer_token: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 10.0,
) -> XUsage | None:
    """Call ``GET /2/usage/tweets`` and return the parsed response, or ``None``
    on any HTTP/parse failure.

    Returns ``None`` (not raise) on errors because the caller — currently
    the digest footer — should degrade gracefully: a transient auth
    error or rate-limit blip shouldn't abort the digest post. The
    structured log records the failure for ops visibility.

    ``client`` is injectable for tests via ``httpx.MockTransport``.
    """
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=timeout)
    try:
        resp = client.get(
            API_BASE + USAGE_TWEETS_PATH,
            headers={"Authorization": f"Bearer {bearer_token}"},
        )
    except httpx.HTTPError as exc:
        log.warning("x.usage.http_error", error=repr(exc))
        if owns_client:
            client.close()
        return None

    try:
        if resp.status_code != 200:
            log.warning(
                "x.usage.non_200",
                status=resp.status_code,
                body=resp.text[:200],
            )
            return None
        payload = resp.json()
    finally:
        if owns_client:
            client.close()

    data = payload.get("data") or {}
    try:
        return XUsage(
            project_usage=int(data["project_usage"]),
            project_cap=int(data["project_cap"]),
            cap_reset_day=int(data["cap_reset_day"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("x.usage.parse_error", error=repr(exc), data=data)
        return None


class XSource(Source):
    """X (Twitter) Recent Search source.

    Pay-per-use API ($0.005/post read). Every call is logged to the
    ``api_usage`` table so the daily cap can be enforced. ``--dry-run``
    callers must never reach :meth:`fetch` — they use
    :meth:`dry_run_state` instead.
    """

    name = "x"

    def __init__(
        self,
        cfg: XSourceConfig,
        storage: Storage,
        *,
        client: httpx.Client | None = None,
        bearer_token: str | None = None,
    ) -> None:
        self.cfg = cfg
        self.storage = storage
        token = bearer_token if bearer_token is not None else os.environ.get("X_BEARER_TOKEN")
        if not token:
            raise SourceInitError(
                "X source requires env var X_BEARER_TOKEN. "
                "Generate one at https://developer.x.com/en/portal/dashboard."
            )
        self._token = token
        self._client = client if client is not None else httpx.Client(timeout=30.0)

    # --------------------------------------------------------------- fetch

    def fetch(self, since_id: str | None = None) -> list[RawItem]:
        items: list[RawItem] = []
        for xq in self.cfg.queries:
            used_today = self._used_today()
            if used_today + self.cfg.max_results_per_query > self.cfg.daily_read_cap:
                log.warning(
                    "x.daily_cap.skip",
                    query_name=xq.name,
                    used_today=used_today,
                    daily_read_cap=self.cfg.daily_read_cap,
                    would_fetch=self.cfg.max_results_per_query,
                )
                continue

            cursor = self.storage.get_cursor(self.name, xq.name)
            new_items, newest_id = self._search(xq, since_id=cursor)
            items.extend(new_items)
            if newest_id is not None:
                self.storage.set_cursor(self.name, xq.name, newest_id)

        return items

    # ------------------------------------------------------------- backfill

    def backfill(self, days: int) -> list[RawItem]:
        if days > BACKFILL_MAX_DAYS:
            log.warning(
                "x.backfill.clamped",
                requested_days=days,
                clamped_to=BACKFILL_MAX_DAYS,
                reason="recent-search-7day-retention",
            )
            days = BACKFILL_MAX_DAYS

        start_time = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        items: list[RawItem] = []
        fetched_count = 0

        for xq in self.cfg.queries:
            used_today = self._used_today()
            if used_today + self.cfg.max_results_per_query > self.cfg.daily_read_cap:
                log.warning(
                    "x.daily_cap.skip",
                    query_name=xq.name,
                    used_today=used_today,
                    daily_read_cap=self.cfg.daily_read_cap,
                )
                continue
            new_items, _ = self._search(xq, start_time=start_time)
            fetched_count += len(new_items)
            items.extend(new_items)

        log.info(
            "backfill.complete",
            source=self.name,
            fetched_count=fetched_count,
            after_client_filter_count=len(items),
            days_requested=days,
        )
        return items

    # --------------------------------------------------------------- dry run

    def dry_run_state(self) -> dict[str, Any]:
        """Return a snapshot of what a real poll would do.

        Called by the CLI's --dry-run path. **Does not make HTTP calls.**
        """
        now = datetime.now(UTC)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return {
            "queries": [
                {
                    "name": q.name,
                    "query": q.query,
                    "since_id": self.storage.get_cursor(self.name, q.name),
                }
                for q in self.cfg.queries
            ],
            "max_results_per_query": self.cfg.max_results_per_query,
            "daily_read_cap": self.cfg.daily_read_cap,
            "used_today": self.storage.sum_api_usage(self.name, start_of_day),
            "used_this_month": self.storage.sum_api_usage(self.name, start_of_month),
        }

    # --------------------------------------------------------------- search

    def _search(
        self,
        xq: XQuery,
        *,
        since_id: str | None = None,
        start_time: str | None = None,
    ) -> tuple[list[RawItem], str | None]:
        params: dict[str, Any] = {
            "query": xq.query,
            "max_results": self.cfg.max_results_per_query,
            "tweet.fields": "created_at,author_id,public_metrics,lang",
            "expansions": "author_id",
            "user.fields": "username,name,verified",
        }
        if since_id is not None:
            params["since_id"] = since_id
        if start_time is not None:
            params["start_time"] = start_time

        payload = self._http_get_json(API_BASE + RECENT_SEARCH_PATH, params=params)
        data = payload.get("data") or []
        users = {u["id"]: u for u in (payload.get("includes") or {}).get("users", [])}
        meta = payload.get("meta") or {}
        result_count = int(meta.get("result_count") or len(data))

        # Record BEFORE building items — we want to log the read even if
        # parsing fails downstream.
        self.storage.record_api_usage(self.name, xq.name, result_count)

        items = [self._to_raw_item(t, users, xq) for t in data]
        newest_id = meta.get("newest_id")
        log.info(
            "x.search",
            query_name=xq.name,
            results=result_count,
            newest_id=newest_id,
        )
        return items, newest_id

    def _used_today(self) -> int:
        start_of_day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return self.storage.sum_api_usage(self.name, start_of_day)

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError,)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        reraise=True,
    )
    def _http_get_json(self, url: str, *, params: dict[str, Any]) -> Any:
        resp = self._client.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {self._token}"},
        )
        resp.raise_for_status()
        return resp.json()

    # ----------------------------------------------------------- conversions

    @staticmethod
    def _to_raw_item(
        tweet: dict[str, Any],
        users_by_id: dict[str, dict[str, Any]],
        xq: XQuery,
    ) -> RawItem:
        author = users_by_id.get(tweet.get("author_id", ""))
        username = author.get("username") if author else None
        url = (
            f"https://x.com/{username}/status/{tweet['id']}"
            if username is not None
            else f"https://x.com/i/web/status/{tweet['id']}"
        )
        return RawItem(
            source="x",
            platform_id=str(tweet["id"]),
            url=url,
            title=_synthesize_tweet_title(tweet["text"]),
            body=tweet["text"],
            author=username,
            created_at=_parse_x_iso(tweet["created_at"]),
            raw_json={
                "tweet": tweet,
                "author": author,
                "query_name": xq.name,
                "group_key": f"x:{xq.name}",
            },
        )


def _parse_x_iso(s: str) -> datetime:
    # X returns e.g. "2026-04-16T12:00:00.000Z"; python 3.12's fromisoformat
    # now accepts the Z suffix directly.
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _synthesize_tweet_title(text: str, *, max_len: int = 80) -> str:
    first_line = text.strip().split("\n", 1)[0]
    if len(first_line) <= max_len:
        return first_line
    return first_line[: max_len - 1] + "…"
