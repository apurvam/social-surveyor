from __future__ import annotations

import os
import re
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

from ..config import GitHubQuery, GitHubSourceConfig
from ..storage import Storage
from ..types import RawItem
from .base import Source, SourceInitError

log = structlog.get_logger(__name__)

API_BASE = "https://api.github.com"
SEARCH_PATH = "/search/issues"

# GitHub's search syntax includes boolean operators and qualifiers we
# don't want polluting our client-side substring match for comment
# filtering. Strip these before tokenizing.
_STOPWORDS = frozenset({"OR", "AND", "NOT", "IN", "TO", "BY", "THE", "A", "AN"})
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{3,}")
_REPO_URL_RE = re.compile(r"^https://api\.github\.com/repos/([^/]+)/([^/]+)$")


class GitHubRateLimitError(httpx.HTTPError):
    """Raised on a 403 that looks like a rate-limit response."""


class GitHubSource(Source):
    """GitHub issue + comment search via the REST API.

    Two search passes per configured query:

    1. Default scope — matches issue body or title.
    2. ``in:comments`` scope — returns issues whose *comments* match. For
       each such issue, we fetch the comments via
       ``/repos/{owner}/{repo}/issues/{number}/comments`` and
       client-side substring-match them against the query terms. Each
       matching comment is stored as its own ``RawItem`` with the parent
       issue's metadata attached to ``raw_json``.

    Per-query cursor is the highest issue ``created_at`` seen. On the
    next poll we add a ``created:>TIMESTAMP`` qualifier so GitHub does
    the filtering for us.
    """

    name = "github"

    def __init__(
        self,
        cfg: GitHubSourceConfig,
        storage: Storage,
        *,
        client: httpx.Client | None = None,
        token: str | None = None,
    ) -> None:
        self.cfg = cfg
        self.storage = storage
        self._cap_warning_logged = False
        resolved_token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        if not resolved_token:
            raise SourceInitError(
                "GitHub source requires env var GITHUB_TOKEN. "
                "Create a personal access token at https://github.com/settings/tokens "
                "(no scopes needed for public search)."
            )
        self._token = resolved_token
        self._client = client if client is not None else httpx.Client(timeout=30.0)
        self._watchlist = {o.lower() for o in self.cfg.orgs_watchlist}
        # Counter for the max_comment_fetches_per_poll guardrail, reset
        # on each fetch()/backfill() call.
        self._comment_fetches_this_call = 0

    # ------------------------------------------------------------------ fetch

    def fetch(self, since_id: str | None = None) -> list[RawItem]:
        self._comment_fetches_this_call = 0
        self._cap_warning_logged = False
        items: list[RawItem] = []

        for gq in self.cfg.queries:
            cursor = self.storage.get_cursor(self.name, self._cursor_key(gq))
            extra: list[str] = []
            if cursor is not None:
                extra.append(f"created:>{cursor}")

            issues_default, max_created_default = self._run_search(gq, extra, scope="default")
            items.extend(self._issue_to_item(i, gq) for i in issues_default)

            issues_in_comments, max_created_comments = self._run_search(
                gq, extra, scope="in:comments"
            )
            for parent_issue in issues_in_comments:
                items.extend(self._fetch_matching_comments(parent_issue, gq))

            new_max = _latest(max_created_default, max_created_comments)
            if new_max is not None:
                self.storage.set_cursor(self.name, self._cursor_key(gq), new_max)

        for item in items:
            self._maybe_warn_watchlist(item)
        return items

    # --------------------------------------------------------------- backfill

    def backfill(self, days: int) -> list[RawItem]:
        self._comment_fetches_this_call = 0
        self._cap_warning_logged = False
        cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        items: list[RawItem] = []
        fetched_count = 0

        for gq in self.cfg.queries:
            extra = [f"created:>{cutoff}"]
            issues_default, _ = self._run_search(gq, extra, scope="default")
            issues_in_comments, _ = self._run_search(gq, extra, scope="in:comments")
            fetched_count += len(issues_default) + len(issues_in_comments)

            items.extend(self._issue_to_item(i, gq) for i in issues_default)
            for parent in issues_in_comments:
                items.extend(self._fetch_matching_comments(parent, gq))

        log.info(
            "backfill.complete",
            source=self.name,
            fetched_count=fetched_count,
            after_client_filter_count=len(items),
            days_requested=days,
        )
        for item in items:
            self._maybe_warn_watchlist(item)
        return items

    # ----------------------------------------------------------------- search

    def _run_search(
        self,
        gq: GitHubQuery,
        extra_qualifiers: list[str],
        *,
        scope: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        q_parts = [gq.q]
        q_parts.extend(extra_qualifiers)
        if gq.type == "issues":
            q_parts.append("type:issue")
        elif gq.type == "prs":
            q_parts.append("type:pr")
        if scope == "in:comments":
            q_parts.append("in:comments")

        params = {
            "q": " ".join(q_parts),
            "sort": "created",
            "order": "desc",
            "per_page": self.cfg.max_results_per_query,
        }
        payload = self._http_get_json(API_BASE + SEARCH_PATH, params=params)
        issues = list(payload.get("items", []))
        log.info(
            "github.search",
            query=gq.q,
            scope=scope,
            results=len(issues),
        )
        latest = max((i["created_at"] for i in issues), default=None)
        return issues, latest

    # ------------------------------------------------------------ comment fetch

    def _fetch_matching_comments(
        self,
        parent_issue: dict[str, Any],
        gq: GitHubQuery,
    ) -> list[RawItem]:
        if self._comment_fetches_this_call >= self.cfg.max_comment_fetches_per_poll:
            # Soft cap hit — stop fetching comments for this poll to
            # protect against runaway cost. Log once per poll.
            if not self._cap_warning_logged:
                log.warning(
                    "github.comments.cap_reached",
                    cap=self.cfg.max_comment_fetches_per_poll,
                    query=gq.q,
                )
                self._cap_warning_logged = True
            return []

        owner, repo = _parse_repo(parent_issue.get("repository_url", ""))
        if owner is None or repo is None:
            log.warning(
                "github.comments.bad_repo_url",
                repository_url=parent_issue.get("repository_url"),
            )
            return []

        number = parent_issue.get("number")
        comments_url = f"{API_BASE}/repos/{owner}/{repo}/issues/{number}/comments"
        self._comment_fetches_this_call += 1
        comments = self._http_get_json(comments_url, params={"per_page": 100})
        if not isinstance(comments, list):
            comments = []

        tokens = _query_tokens(gq.q)
        matched = [c for c in comments if _body_matches_any_token(c.get("body") or "", tokens)]

        # Log the counts — when search returns issues but client-side
        # match extracts nothing, we want to know.
        log.info(
            "github.comments.fetched",
            issue_number=number,
            repo=f"{owner}/{repo}",
            comments_total=len(comments),
            comments_matched=len(matched),
        )
        return [self._comment_to_item(c, parent_issue, gq) for c in matched]

    # ------------------------------------------------------------- conversions

    @staticmethod
    def _issue_to_item(issue: dict[str, Any], gq: GitHubQuery) -> RawItem:
        return RawItem(
            source="github",
            platform_id=f"github-issue-{issue['id']}",
            url=issue["html_url"],
            title=issue.get("title") or "(no title)",
            body=issue.get("body"),
            author=(issue.get("user") or {}).get("login"),
            created_at=_parse_github_iso(issue["created_at"]),
            raw_json={
                **issue,
                "search_scope": "default",
                "matched_query": gq.q,
                "is_pr": "pull_request" in issue,
            },
        )

    @staticmethod
    def _comment_to_item(
        comment: dict[str, Any],
        parent_issue: dict[str, Any],
        gq: GitHubQuery,
    ) -> RawItem:
        parent_title = parent_issue.get("title") or ""
        return RawItem(
            source="github",
            platform_id=f"github-comment-{comment['id']}",
            url=comment["html_url"],
            title=f"Comment on: {parent_title}",
            body=comment.get("body"),
            author=(comment.get("user") or {}).get("login"),
            created_at=_parse_github_iso(comment["created_at"]),
            raw_json={
                **comment,
                "search_scope": "in:comments",
                "matched_query": gq.q,
                "parent_issue": {
                    "id": parent_issue.get("id"),
                    "number": parent_issue.get("number"),
                    "title": parent_issue.get("title"),
                    "state": parent_issue.get("state"),
                    "html_url": parent_issue.get("html_url"),
                    "repository_url": parent_issue.get("repository_url"),
                    "is_pr": "pull_request" in parent_issue,
                },
            },
        )

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _cursor_key(gq: GitHubQuery) -> str:
        return f"{gq.type}:{gq.q}"

    def _maybe_warn_watchlist(self, item: RawItem) -> None:
        if not self._watchlist:
            return
        owner = _owner_for_item(item)
        if owner is not None and owner.lower() in self._watchlist:
            log.warning(
                "github.watchlist_match",
                owner=owner,
                platform_id=item.platform_id,
                url=item.url,
            )

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError,)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        reraise=True,
    )
    def _http_get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        resp = self._client.get(url, params=params, headers=headers)
        if resp.status_code == 403 and _looks_like_rate_limit(resp):
            raise GitHubRateLimitError(
                f"GitHub rate limited: {resp.headers.get('X-RateLimit-Remaining')} "
                f"remaining; reset at {resp.headers.get('X-RateLimit-Reset')}"
            )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------- module-level utils


def _parse_github_iso(s: str) -> datetime:
    # GitHub timestamps are ISO-8601 ending in Z; normalize to +00:00.
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _parse_repo(repository_url: str) -> tuple[str | None, str | None]:
    m = _REPO_URL_RE.match(repository_url or "")
    if not m:
        return None, None
    return m.group(1), m.group(2)


def _owner_for_item(item: RawItem) -> str | None:
    rj = item.raw_json or {}
    repo_url = rj.get("repository_url") or (rj.get("parent_issue") or {}).get("repository_url")
    if repo_url:
        owner, _ = _parse_repo(repo_url)
        return owner
    return None


def _query_tokens(q: str) -> list[str]:
    """Extract substring-matchable tokens from a GitHub search query.

    Boolean operators and short fillers are dropped. Returns lowercase
    tokens; callers should lowercase the haystack too.
    """
    raw = _TOKEN_RE.findall(q)
    return [t.lower() for t in raw if t.upper() not in _STOPWORDS]


def _body_matches_any_token(body: str, tokens: list[str]) -> bool:
    if not tokens:
        return False
    hay = body.lower()
    return any(t in hay for t in tokens)


def _looks_like_rate_limit(resp: httpx.Response) -> bool:
    if resp.headers.get("X-RateLimit-Remaining") == "0":
        return True
    try:
        msg = (resp.json() or {}).get("message", "")
    except Exception:
        msg = resp.text
    return "rate limit" in msg.lower() or "abuse" in msg.lower()


def _latest(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b
