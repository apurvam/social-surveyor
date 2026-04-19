"""`social-surveyor ingest --url <url>` — capture a manually-supplied item.

Loop 1 of the correction workflow: the operator spots something
interesting elsewhere that should have been in the digest, pastes the
URL here, and the tool fetches → classifies → prints. No routing
happens — the operator is already in the loop, so there's no need to
re-surface via Slack.

URL → source detection is pattern-matched (string rules on host and
path). Started simple per the Session 4 decision; upgrades to a URL-
parsing library if pattern matching breaks on edge cases.

Fetch endpoints:

- **Hacker News**: Algolia's free item-lookup endpoint, so ingesting
  HN items has no cost.
- **Reddit**: the public ``/comments/<id>.json`` view — same data the
  RSS source already uses variants of. Unauthenticated.
- **X**: the paid ``GET /2/tweets`` endpoint. Counts toward the daily
  read cap — we record the usage row so costs stay accounted for.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import structlog
import typer

from .cli_classify import run_classify
from .storage import Storage
from .types import RawItem

log = structlog.get_logger(__name__)

_HN_ALGOLIA_ITEM_URL = "https://hn.algolia.com/api/v1/items/{id_}"
_REDDIT_COMMENTS_URL = "https://www.reddit.com/comments/{id_}.json"
_X_TWEETS_URL = "https://api.twitter.com/2/tweets"
_X_TWEET_URL_TEMPLATE = "https://x.com/{user}/status/{id_}"

# URL-matcher patterns. Narrow enough not to swallow unrelated links
# (e.g. a random news.ycombinator.com submission page doesn't match
# the item?id= form).
_HN_ITEM_RE = re.compile(r"^https?://news\.ycombinator\.com/item\?")
_REDDIT_HOST_RE = re.compile(r"^https?://(?:www\.|old\.|new\.)?reddit\.com/")
_REDDIT_COMMENT_ID_RE = re.compile(r"/comments/([a-z0-9]+)(?:/|$)", re.IGNORECASE)
_X_STATUS_RE = re.compile(r"^https?://(?:x|twitter)\.com/[^/]+/status/(\d+)")


def run_ingest(
    project: str,
    db_path: Path,
    projects_root: Path,
    *,
    url: str,
    http_client: httpx.Client | None = None,
    anthropic_client: Any = None,
    echo_fn: Any = typer.echo,
    sv_command: str = "social-surveyor",
) -> dict[str, Any]:
    """Fetch ``url`` into the DB and classify it.

    Returns a dict with ``source``, ``item_id``, and ``classification``
    (the last may be None if classify failed).
    """
    if not db_path.parent.exists():
        db_path.parent.mkdir(parents=True, exist_ok=True)

    client = http_client if http_client is not None else httpx.Client(timeout=15.0)
    owns_client = http_client is None

    try:
        source = _detect_source(url)
        if source == "hackernews":
            item = _fetch_hn(url, client)
        elif source == "reddit":
            item = _fetch_reddit(url, client)
        elif source == "x":
            item = _fetch_x(url, client, db_path)
        else:  # pragma: no cover — guarded by _detect_source
            raise typer.BadParameter(f"unsupported source for url: {url}")

        with Storage(db_path) as db:
            inserted = db.upsert_item(item)
        item_id = f"{item.source}:{item.platform_id}"

        if inserted:
            echo_fn(f"ingested new item {item_id} — {item.title[:100]!r}")
        else:
            echo_fn(f"item {item_id} already in DB — will re-classify if new prompt_version")

        # Classify under the active prompt_version. run_classify skips
        # items with a cached classification for that version, so a
        # repeat ingest is a no-op on the classifier.
        run_classify(
            project,
            db_path,
            projects_root,
            item_id=item_id,
            limit=None,
            prompt_version_override=None,
            dry_run=False,
            client=anthropic_client,
            echo_fn=echo_fn,
        )

        echo_fn("")
        echo_fn("This item is now in the DB. Label or silence:")
        echo_fn(f"  {sv_command} label --project {project} --item-id {item_id}")
        echo_fn(f"  {sv_command} silence --project {project} --item-id {item_id}")

        return {"source": source, "item_id": item_id, "inserted": inserted}
    finally:
        if owns_client:
            client.close()


# --- source detection --------------------------------------------------------


def _detect_source(url: str) -> str:
    if _HN_ITEM_RE.match(url):
        return "hackernews"
    if _REDDIT_HOST_RE.match(url):
        return "reddit"
    if _X_STATUS_RE.match(url):
        return "x"
    raise typer.BadParameter(
        f"unsupported source for url: {url!r}. "
        "Supported: news.ycombinator.com/item, reddit.com/comments/<id>, "
        "x.com/<user>/status/<id>."
    )


# --- HN ---------------------------------------------------------------------


def _fetch_hn(url: str, client: httpx.Client) -> RawItem:
    qs = parse_qs(urlparse(url).query)
    id_list = qs.get("id", [])
    if not id_list:
        raise typer.BadParameter(f"HN url missing ?id=: {url!r}")
    hn_id = id_list[0]
    resp = client.get(_HN_ALGOLIA_ITEM_URL.format(id_=hn_id))
    if resp.status_code != 200:
        raise typer.BadParameter(
            f"HN fetch failed for id {hn_id}: {resp.status_code} {resp.text[:200]}"
        )
    d = resp.json()
    title = d.get("title") or d.get("story_title") or "(no title)"
    body_parts: list[str] = []
    if d.get("text"):
        body_parts.append(_strip_html(d["text"]))
    created_at = _hn_created_at(d)
    return RawItem(
        source="hackernews",
        platform_id=str(d.get("id") or hn_id),
        url=f"https://news.ycombinator.com/item?id={hn_id}",
        title=_strip_html(title),
        body="\n\n".join(body_parts) or None,
        author=d.get("author"),
        created_at=created_at,
        raw_json={
            "id": d.get("id"),
            "type": d.get("type"),
            "ingested_manually": True,
        },
    )


def _hn_created_at(d: dict[str, Any]) -> datetime:
    # Algolia item endpoint returns created_at as ISO string. Keep
    # robust to created_at_i (unix epoch) as a fallback.
    if d.get("created_at"):
        try:
            return datetime.fromisoformat(d["created_at"].replace("Z", "+00:00"))
        except ValueError:
            pass
    if d.get("created_at_i") is not None:
        return datetime.fromtimestamp(int(d["created_at_i"]), tz=UTC)
    return datetime.now(UTC)


# --- Reddit -----------------------------------------------------------------


def _fetch_reddit(url: str, client: httpx.Client) -> RawItem:
    m = _REDDIT_COMMENT_ID_RE.search(urlparse(url).path)
    if m is None:
        raise typer.BadParameter(f"Reddit url missing /comments/<id>: {url!r}")
    post_id = m.group(1)
    # User-Agent matters — Reddit rate-limits generic agents aggressively.
    headers = {
        "User-Agent": "social-surveyor-ingest/0.1 (by /u/social-surveyor)",
    }
    resp = client.get(_REDDIT_COMMENTS_URL.format(id_=post_id), headers=headers)
    if resp.status_code != 200:
        raise typer.BadParameter(
            f"Reddit fetch failed for id {post_id}: {resp.status_code} {resp.text[:200]}"
        )
    payload = resp.json()
    # Response is [post_listing, comments_listing]; first listing's
    # first child is the post itself.
    try:
        post = payload[0]["data"]["children"][0]["data"]
    except (KeyError, IndexError, TypeError) as exc:
        raise typer.BadParameter(f"Reddit response for {post_id} didn't parse: {exc}") from exc
    created_at = datetime.fromtimestamp(float(post["created_utc"]), tz=UTC)
    return RawItem(
        source="reddit",
        platform_id=f"t3_{post_id}",
        url=f"https://www.reddit.com{post['permalink']}",
        title=post.get("title") or "(no title)",
        body=post.get("selftext") or None,
        author=post.get("author"),
        created_at=created_at,
        raw_json={
            "subreddit": post.get("subreddit"),
            "id": post.get("id"),
            "ingested_manually": True,
        },
    )


# --- X ----------------------------------------------------------------------


def _fetch_x(url: str, client: httpx.Client, db_path: Path) -> RawItem:
    m = _X_STATUS_RE.match(url)
    if m is None:
        raise typer.BadParameter(f"X url missing /status/<id>: {url!r}")
    tweet_id = m.group(1)
    token = os.environ.get("X_BEARER_TOKEN")
    if not token:
        raise typer.BadParameter("X_BEARER_TOKEN not set — required for ingesting X urls.")
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "ids": tweet_id,
        "tweet.fields": "created_at,public_metrics,author_id,text",
        "expansions": "author_id",
        "user.fields": "username,verified",
    }
    resp = client.get(_X_TWEETS_URL, headers=headers, params=params)
    if resp.status_code != 200:
        raise typer.BadParameter(
            f"X fetch failed for id {tweet_id}: {resp.status_code} {resp.text[:200]}"
        )
    payload = resp.json()
    tweets = payload.get("data") or []
    if not tweets:
        raise typer.BadParameter(
            f"X returned no tweet for id {tweet_id} — deleted, protected, or id mismatch."
        )
    tweet = tweets[0]
    users = {u["id"]: u for u in (payload.get("includes", {}).get("users") or [])}
    author = users.get(tweet.get("author_id"), {})
    username = author.get("username", "")

    # Record the paid read against the cap ledger even in ingest mode —
    # X reads are X reads regardless of who triggered them.
    with Storage(db_path) as db:
        db.record_api_usage("x", "ingest", 1)

    created_at = datetime.fromisoformat(tweet["created_at"].replace("Z", "+00:00"))
    return RawItem(
        source="x",
        platform_id=str(tweet["id"]),
        url=_X_TWEET_URL_TEMPLATE.format(user=username or "unknown", id_=tweet["id"]),
        title=(tweet.get("text") or "")[:200],
        body=tweet.get("text") or None,
        author=username or None,
        created_at=created_at,
        raw_json={
            "tweet": tweet,
            "author": author,
            "ingested_manually": True,
        },
    )


# --- helpers ----------------------------------------------------------------


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    import html

    return html.unescape(_TAG_RE.sub("", text))


__all__ = ["run_ingest"]
