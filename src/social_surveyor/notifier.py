"""Slack Block Kit payload builders + webhook client.

Separation of concerns:

- **Builders** (:func:`build_immediate_alert`, :func:`build_digest`) are
  pure: they take flat data and return Block Kit-shaped dicts. No
  network, no storage — they're what the tests cover.
- **Poster** (:func:`post_to_slack`) is the only I/O surface. Thin. It
  POSTs a payload to an incoming webhook and raises on non-200.

Why this split: the Block Kit Builder at app.slack.com is the fastest
way to iterate on formatting, and it takes JSON in and renders Slack
out. Keeping the builders pure means we can paste their output directly
into the Builder to tweak wording without redeploying anything.

Design notes specific to this session:

- **Level A only** — standard incoming webhooks. The POST response is
  the literal text ``ok``; there is no message ``ts``, so threading is
  not available. Collapsed-category detail lives in the CLI (``sv
  digest --category <cat>``) rather than threaded replies. A future
  session can graduate to a bot-token Slack app and switch the posting
  surface without touching these builders.
- **Top-5 per category** — the main digest shows the top 5 items per
  category by urgency (then recency). If a category has more, a hint
  line in that section points to the CLI inspection command. Keeps
  the digest's visual footprint consistent day to day regardless of
  per-category volume.
- **Custom emoji are optional** — ``:reddit:`` / ``:hn:`` / ``:x:`` /
  ``:github:`` are assumed uploaded to the workspace. If they aren't,
  Slack renders the literal text, which is an acceptable fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

# --- constants ---------------------------------------------------------------

# Left-edge color bar for immediate alerts (Slack attachment `color`).
# Hex picks track a tab10-ish palette so color meaning is stable across
# pasted screenshots / docs. active_practitioner listed for completeness —
# it's non-alert-worthy and should never fire an alert, but the builder
# handles it defensively rather than raising mid-loop.
CATEGORY_COLORS: dict[str, str] = {
    "cost_complaint": "#d62728",
    "self_host_intent": "#1f77b4",
    "competitor_pain": "#ff7f0e",
    "active_practitioner": "#2ca02c",
    "neutral_discussion": "#7f7f7f",
    "tutorial_or_marketing": "#9467bd",
    "off_topic": "#8c564b",
}

CATEGORY_EMOJI: dict[str, str] = {
    "cost_complaint": "💰",
    "self_host_intent": "🛠️",
    "competitor_pain": "⚠️",
    "active_practitioner": "🔧",
    "neutral_discussion": "💬",
    "tutorial_or_marketing": "📘",
    "off_topic": "🚫",
}

# Section order in the digest. Fixed so day-to-day scannability doesn't
# depend on which categories happened to fire that day. Alert-worthy
# first, then the relationship-building bucket, then context.
DIGEST_CATEGORY_ORDER: tuple[str, ...] = (
    "cost_complaint",
    "self_host_intent",
    "competitor_pain",
    "active_practitioner",
    "neutral_discussion",
    "tutorial_or_marketing",
    "off_topic",
)

# Per-category item cap in the main digest message. Overflow gets a
# single hint line pointing at the CLI inspection command.
TOP_N_PER_CATEGORY = 5

# Truncation bounds. 120 chars is what fits in a Slack section at a
# normal window width; 200 on bodies is enough to skim without eating
# the whole screen.
TITLE_MAX_CHARS = 120
BODY_MAX_CHARS = 200

_FALLBACK_COLOR = "#555555"

_SOURCE_EMOJI: dict[str, str] = {
    "reddit": ":reddit:",
    "hackernews": ":hn:",
    "x": ":x:",
    "github": ":github:",
}


# --- input shapes ------------------------------------------------------------


@dataclass(frozen=True)
class NotifierConfig:
    """Project-scoped config the builders need to emit correction commands.

    ``sv_command`` defaults to ``social-surveyor``. Forks that adopt the
    ``sv`` shell alias (documented in the README) set it to ``"sv"`` so
    the copy-paste lines are terse.
    """

    project: str
    sv_command: str = "social-surveyor"


@dataclass(frozen=True)
class NotifierItem:
    """Flat per-item input to the builders.

    Pre-computed on the caller side so the builders stay pure. Fields
    map directly onto what the eventual Slack message needs; any
    source-specific or classifier-specific quirks are resolved by the
    caller before we get here.
    """

    item_id: str
    source: str
    category: str
    urgency: int
    title: str
    body: str | None
    author: str | None
    url: str | None
    created_at: datetime
    reasoning: str | None = None
    # For digest "alerted earlier today" section. None means not alerted
    # in this window; a timestamp means it was.
    alerted_at: datetime | None = None
    # True when this item was silenced within the digest window —
    # shown with 🔕 marker in its category. Older silences filter the
    # item out entirely upstream, so this flag is only ever true inside
    # the window.
    silenced: bool = False


@dataclass(frozen=True)
class DigestStats:
    """Cost + accuracy footer for the digest."""

    day: date
    haiku_cost_usd: float
    x_cost_usd: float
    total_labeled: int
    # Latest eval accuracy, or None if no eval has been run / recorded.
    accuracy_pct: float | None = None


# --- builders ----------------------------------------------------------------


def build_immediate_alert(item: NotifierItem, config: NotifierConfig) -> dict[str, Any]:
    """Return the Block Kit payload for a single high-urgency item.

    Uses the attachment-with-color shape (not pure Block Kit) so the
    message picks up a category-colored left-edge bar. Everything
    inside the attachment is standard Block Kit.
    """
    color = CATEGORY_COLORS.get(item.category, _FALLBACK_COLOR)

    header = f"{_source_label(item.source)} *{item.category}* · `urgency {item.urgency}`"
    quote_lines: list[str] = [f"> *{_truncate(item.title or '(no title)', TITLE_MAX_CHARS)}*"]
    if item.body:
        for line in _truncate(item.body, BODY_MAX_CHARS).splitlines() or [""]:
            quote_lines.append(f"> {line}")

    context_bits: list[str] = [
        f"_by `{item.author or 'unknown'}` · {_relative_time(item.created_at)}_"
    ]
    if item.url:
        context_bits.append(f"<{item.url}|Open in {item.source}>")
    context_line = "  ·  ".join(context_bits)

    section_text = "\n".join([header, *quote_lines, "", context_line])
    if item.reasoning:
        section_text += f"\n_{_escape_mrkdwn(item.reasoning)}_"

    correction_block = (
        f"{config.sv_command} label --project {config.project} --item-id {item.item_id} "
        f"--category <cat> --urgency <n>\n"
        f"{config.sv_command} silence --project {config.project} --item-id {item.item_id}"
    )

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": section_text},
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"Item ID: `{item.item_id}`"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"```\n{correction_block}\n```"},
        },
    ]

    return {
        "attachments": [
            {
                "color": color,
                "blocks": blocks,
            }
        ]
    }


def build_digest(
    items: list[NotifierItem],
    stats: DigestStats,
    config: NotifierConfig,
) -> dict[str, Any]:
    """Return the Block Kit payload for the daily digest message.

    Structure (spec + session-4 decisions):

    1. Header line with day, counts, alerted-earlier count, and cost
    2. Alerted-earlier section (if any items alerted in the window)
    3. One section per category in :data:`DIGEST_CATEGORY_ORDER`,
       skipping empty categories. Each section shows the top 5 items
       by urgency (then recency), with an overflow hint if the
       category has more than 5.
    4. Correction footer with the three copy-paste commands
    5. Cost footer, including a pointer to the ``digest --category``
       CLI command
    """
    alerted = [i for i in items if i.alerted_at is not None]
    # Alerted-earlier items appear only in their own section, not in
    # category sections. Without this, they'd render twice in the same
    # digest — the spec's "K alerted earlier" call-out is meant as a
    # lift, not a duplicate listing.
    unalerted = [i for i in items if i.alerted_at is None]
    by_category: dict[str, list[NotifierItem]] = {c: [] for c in DIGEST_CATEGORY_ORDER}
    for item in unalerted:
        bucket = by_category.setdefault(item.category, [])
        bucket.append(item)

    categories_with_items = [c for c in DIGEST_CATEGORY_ORDER if by_category[c]]
    # Any categories outside the known order (defensive — fork with a
    # custom taxonomy) appear after the known ones, alphabetically.
    unknown = sorted(c for c in by_category if c not in DIGEST_CATEGORY_ORDER and by_category[c])
    ordered_categories = [*categories_with_items, *unknown]

    blocks: list[dict[str, Any]] = []

    # --- header ---
    total_cost = stats.haiku_cost_usd + stats.x_cost_usd
    header_text = (
        f"📊 *Digest for {stats.day.isoformat()}* · "
        f"{len(items)} items across {len(ordered_categories)} categories · "
        f"{len(alerted)} alerted earlier · "
        f"cost ${total_cost:.2f}"
    )
    if not items:
        # Zero-items case still sends a message so we know the pipeline
        # is alive. Same header shape with a friendly body.
        header_text = f"📊 *Digest for {stats.day.isoformat()}* · no new items in the last 24h"
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": header_text}})

    if not items:
        blocks.extend(_cost_and_correction_footer(stats, config))
        return {"blocks": blocks}

    # --- alerted-earlier section ---
    if alerted:
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "🔔 *Alerted earlier today*"},
            }
        )
        for item in sorted(
            alerted, key=lambda i: (-(i.urgency), -(i.alerted_at or datetime.now(UTC)).timestamp())
        ):
            blocks.append(_alerted_earlier_block(item))

    # --- per-category sections ---
    for cat in ordered_categories:
        cat_items = by_category[cat]
        # Sort by urgency desc, then by created_at desc (recency secondary).
        cat_items_sorted = sorted(
            cat_items,
            key=lambda i: (-(i.urgency), -i.created_at.timestamp()),
        )
        top = cat_items_sorted[:TOP_N_PER_CATEGORY]
        overflow = len(cat_items_sorted) - len(top)

        blocks.append({"type": "divider"})
        emoji = CATEGORY_EMOJI.get(cat, "•")
        total_in_cat = len(cat_items_sorted)
        if overflow > 0:
            header_line = (
                f"{emoji} *{cat}* · {total_in_cat} items (showing top {len(top)} by urgency)"
            )
        else:
            header_line = f"{emoji} *{cat}* · {total_in_cat} items"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": header_line}})
        for item in top:
            blocks.append(_digest_item_block(item))
        if overflow > 0:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": (
                                f"_{overflow} more {cat} items — run "
                                f"`{config.sv_command} digest --project "
                                f"{config.project} --category {cat}` for the full list_"
                            ),
                        }
                    ],
                }
            )

    blocks.extend(_cost_and_correction_footer(stats, config))
    return {"blocks": blocks}


# --- block helpers -----------------------------------------------------------


def _alerted_earlier_block(item: NotifierItem) -> dict[str, Any]:
    """Compact single-item block for the 'alerted earlier' digest section."""
    alerted_at = item.alerted_at or item.created_at
    lines = [
        f"{_source_label(item.source)} *{item.category}* · `u={item.urgency}`",
        f"> *{_truncate(item.title or '(no title)', TITLE_MAX_CHARS)}*",
        f"_by `{item.author or 'unknown'}` · alerted at {alerted_at.strftime('%H:%M')}_",
        f"Item ID: `{item.item_id}`",
    ]
    return {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}


def _digest_item_block(item: NotifierItem) -> dict[str, Any]:
    """One compact line per item inside a category section."""
    silenced_prefix = "🔕 " if item.silenced else ""
    title = _truncate(item.title or "(no title)", TITLE_MAX_CHARS)
    author = item.author or "unknown"
    line = (
        f"{silenced_prefix}{_source_label(item.source)} `u={item.urgency}` "
        f"*{title}* _by `{author}`_ · `{item.item_id}`"
    )
    return {"type": "section", "text": {"type": "mrkdwn", "text": line}}


def _cost_and_correction_footer(stats: DigestStats, config: NotifierConfig) -> list[dict[str, Any]]:
    """Shared tail: correction block → cost/accuracy line → CLI pointer."""
    correction_text = (
        f"✏️  *Correct a classification* · replace `<id>`, `<cat>`, and `<n>`\n"
        f"```\n"
        f"{config.sv_command} label --project {config.project} "
        f"--item-id <id> --category <cat> --urgency <n>\n"
        f"{config.sv_command} silence --project {config.project} --item-id <id>\n"
        f"{config.sv_command} ingest --project {config.project} --url <url>\n"
        f"```\n"
        f"Categories: " + " · ".join(DIGEST_CATEGORY_ORDER)
    )

    accuracy_bit = (
        f" · {stats.accuracy_pct:.1f}% accuracy" if stats.accuracy_pct is not None else ""
    )
    cost_text = (
        f"_Today: ${stats.haiku_cost_usd:.2f} Haiku · "
        f"${stats.x_cost_usd:.2f} X · "
        f"{stats.total_labeled} items labeled{accuracy_bit}_\n"
        f"_For full category details: `{config.sv_command} digest "
        f"--project {config.project} --category <cat>`_"
    )

    return [
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": correction_text}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": cost_text}},
    ]


# --- text helpers ------------------------------------------------------------


def _truncate(text: str, n: int) -> str:
    """Trim to n chars, appending an ellipsis if truncated.

    Counts the ellipsis toward the limit so the result is always ≤ n
    characters. Safe on short strings (no-op when ``len(text) <= n``).
    """
    if len(text) <= n:
        return text
    if n <= 1:
        return "…"
    return text[: n - 1].rstrip() + "…"


def _source_label(source: str) -> str:
    """Custom-emoji shortcode for a source, or a bracketed fallback.

    If the workspace hasn't uploaded the four custom emoji, Slack
    renders the shortcode as literal text — an acceptable fallback
    that still reads fine on the eye.
    """
    return _SOURCE_EMOJI.get(source, f"[{source}]")


def _relative_time(moment: datetime) -> str:
    """'3 hours ago' / '2 days ago' shape for context lines.

    Uses UTC now so timestamps are stable across timezones — the
    digest's human-facing time is in the header, this is just a
    "roughly when" for context.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - moment
    if delta < timedelta(minutes=1):
        return "just now"
    if delta < timedelta(hours=1):
        mins = int(delta.total_seconds() // 60)
        return f"{mins} minute{'s' if mins != 1 else ''} ago"
    if delta < timedelta(days=1):
        hours = int(delta.total_seconds() // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = delta.days
    return f"{days} day{'s' if days != 1 else ''} ago"


def _escape_mrkdwn(text: str) -> str:
    """Escape the three characters Slack's mrkdwn treats as special.

    Block Kit's ``mrkdwn`` type is permissive about most punctuation but
    the ``&``/``<``/``>`` trio needs escaping to avoid accidental entity
    or link-syntax interpretation. Other metacharacters (``*``, ``_``,
    backtick) are left as-is because classifier reasoning rarely
    contains them and escaping them mangles legitimate emphasis.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- posting -----------------------------------------------------------------


class SlackPostError(RuntimeError):
    """Raised when Slack's webhook returns a non-200 response."""


def post_to_slack(
    payload: dict[str, Any],
    webhook_url: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 10.0,
) -> None:
    """POST ``payload`` to Slack's incoming webhook. Raise on non-200.

    No return value: standard incoming webhooks return the literal
    text ``ok`` on success with no ``ts``, so we can't thread replies.
    Callers who need a thread should use a bot-token Slack app and
    call ``chat.postMessage`` directly — that's a future session's
    work.

    ``client`` is injectable for tests using httpx's MockTransport.
    Default is a short-lived client per call; fine at digest cadence
    (once per project per day) plus occasional immediate alerts.
    """
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=timeout)
    try:
        resp = client.post(webhook_url, json=payload)
    finally:
        if owns_client:
            client.close()

    if resp.status_code != 200:
        raise SlackPostError(f"Slack webhook POST failed: {resp.status_code} {resp.text[:200]}")


__all__ = [
    "CATEGORY_COLORS",
    "CATEGORY_EMOJI",
    "DIGEST_CATEGORY_ORDER",
    "TOP_N_PER_CATEGORY",
    "DigestStats",
    "NotifierConfig",
    "NotifierItem",
    "SlackPostError",
    "build_digest",
    "build_immediate_alert",
    "post_to_slack",
]
