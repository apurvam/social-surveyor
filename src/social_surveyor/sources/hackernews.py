from __future__ import annotations

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

from ..config import HackerNewsSourceConfig
from ..storage import Storage
from ..types import RawItem
from .base import Source

log = structlog.get_logger(__name__)

SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
HN_ITEM_URL_TEMPLATE = "https://news.ycombinator.com/item?id={id_}"


class HackerNewsSource(Source):
    """Hacker News search via the public Algolia endpoint.

    Free and unauthenticated. Per-query cursor is the highest
    ``created_at_i`` seen; persisted in ``source_cursors`` so
    subsequent polls only ask for newer items (``numericFilters``).
    """

    name = "hackernews"

    def __init__(
        self,
        cfg: HackerNewsSourceConfig,
        storage: Storage,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.cfg = cfg
        self.storage = storage
        self._client = client if client is not None else httpx.Client(timeout=20.0)

    def fetch(self, since_id: str | None = None) -> list[RawItem]:
        items: list[RawItem] = []
        tags_filter = self._tags_filter()
        for query in self.cfg.queries:
            cursor = self.storage.get_cursor(self.name, query)
            params: dict[str, Any] = {
                "query": query,
                "hitsPerPage": self.cfg.max_results_per_query,
                "tags": tags_filter,
            }
            if cursor is not None:
                params["numericFilters"] = f"created_at_i>{cursor}"

            hits = self._get_hits(params)
            new_max_cursor = max((int(h["created_at_i"]) for h in hits), default=None)
            query_items = [self._to_raw_item(h) for h in hits if self._is_allowed(h)]
            items.extend(query_items)

            log.info(
                "hackernews.fetch",
                query=query,
                results=len(hits),
                kept=len(query_items),
                cursor_advanced=new_max_cursor is not None,
            )
            if new_max_cursor is not None:
                self.storage.set_cursor(self.name, query, str(new_max_cursor))

        return items

    def backfill(self, days: int) -> list[RawItem]:
        cutoff_ts = int((datetime.now(UTC) - timedelta(days=days)).timestamp())
        tags_filter = self._tags_filter()
        items: list[RawItem] = []
        fetched_count = 0
        for query in self.cfg.queries:
            params: dict[str, Any] = {
                "query": query,
                "hitsPerPage": self.cfg.max_results_per_query,
                "tags": tags_filter,
                "numericFilters": f"created_at_i>{cutoff_ts}",
            }
            hits = self._get_hits(params)
            fetched_count += len(hits)
            kept = [self._to_raw_item(h) for h in hits if self._is_allowed(h)]
            items.extend(kept)
            log.debug(
                "hackernews.backfill.search",
                query=query,
                results=len(hits),
                kept=len(kept),
            )

        log.info(
            "backfill.complete",
            source=self.name,
            fetched_count=fetched_count,
            after_client_filter_count=len(items),
            days_requested=days,
        )
        return items

    # Algolia's `tags` param uses comma-separated OR semantics for a
    # single tag category, which is what we want for story/comment mix.
    def _tags_filter(self) -> str:
        if len(self.cfg.tags) == 1:
            return self.cfg.tags[0]
        return "(" + ",".join(self.cfg.tags) + ")"

    def _is_allowed(self, hit: dict[str, Any]) -> bool:
        hit_tags = set(hit.get("_tags", []))
        return any(t in hit_tags for t in self.cfg.tags)

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError,)),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    def _get_hits(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        resp = self._client.get(SEARCH_URL, params=params)
        resp.raise_for_status()
        return list(resp.json().get("hits", []))

    @staticmethod
    def _to_raw_item(hit: dict[str, Any]) -> RawItem:
        tags = set(hit.get("_tags", []))
        is_comment = "comment" in tags
        object_id = str(hit["objectID"])
        created_at = datetime.fromtimestamp(int(hit["created_at_i"]), tz=UTC)
        author = hit.get("author")

        if is_comment:
            parent_id = hit.get("story_id") or hit.get("parent_id")
            title = (
                f"Comment by {author or 'anonymous'} on HN #{parent_id}"
                if parent_id is not None
                else f"Comment by {author or 'anonymous'} on HN"
            )
            body = hit.get("comment_text") or ""
            url = HN_ITEM_URL_TEMPLATE.format(id_=object_id)
        else:
            title = hit.get("title") or f"(untitled story {object_id})"
            # Prefer the external URL if present — matches session 2 spec.
            url = hit.get("url") or HN_ITEM_URL_TEMPLATE.format(id_=object_id)
            body = hit.get("story_text") or None

        return RawItem(
            source="hackernews",
            platform_id=object_id,
            url=url,
            title=title,
            body=body if body else None,
            author=author,
            created_at=created_at,
            raw_json=hit,
        )
