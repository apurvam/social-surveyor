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

Design notes:

- **Level A only** — standard incoming webhooks. The POST response is
  the literal text ``ok``; there is no message ``ts``, so threading is
  not available. A future session can graduate to a bot-token Slack
  app and switch the posting surface without touching these builders.
- **Top-5 per category** — the main digest shows the top 5 items per
  category by urgency (then recency). If a category has more, a hint
  line names the overflow count. Keeps the digest's visual footprint
  consistent day to day regardless of per-category volume.
- **No CLI copy-paste lines** — item IDs ship in every card (in a
  trailing monospace span) but the ``sv label/silence/ingest/digest``
  commands don't. A coding agent driving the correction workflow can
  construct the command from the id + intent faster than a human can
  read the line; the static copy-paste text was noise for both
  audiences.
- **Custom emoji are optional** — ``:reddit:`` / ``:hn:`` / ``:twitter:`` /
  ``:github:`` are assumed uploaded to the workspace. If they aren't,
  Slack renders the literal text, which is an acceptable fallback. We
  use ``:twitter:`` rather than Slack's built-in ``:x:``, which renders
  as a red X mark and reads wrong next to a news item.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

# --- constants ---------------------------------------------------------------

# Left-edge color bar for immediate alerts (Slack attachment `color`).
# Hex picks track a tab10-ish palette so color meaning is stable across
# pasted screenshots / docs. This is keyed by the opendata project's
# taxonomy; fork projects with different category ids fall through to
# `_FALLBACK_COLOR` (gray) until a per-project color config is added.
# Digest ordering, by contrast, is fully project-driven — see
# `NotifierConfig.category_order` below.
CATEGORY_COLORS: dict[str, str] = {
    "cost_complaint": "#d62728",
    "self_host_intent": "#1f77b4",
    "competitor_pain": "#ff7f0e",
    "active_practitioner": "#2ca02c",
    "neutral_discussion": "#7f7f7f",
    "tutorial_or_marketing": "#9467bd",
    "off_topic": "#8c564b",
}

# Per-category item cap in the main digest message. Overflow gets a
# single hint line pointing at the CLI inspection command.
TOP_N_PER_CATEGORY = 5

# Slack Block Kit enforces a hard ceiling of 50 blocks per message; a
# POST with 51+ blocks is rejected with 400 invalid_blocks. We target a
# lower budget to leave headroom for future block additions and for
# renderer quirks (some clients reject pre-limit payloads on their own
# internal heuristics). If the naive build exceeds this, trailing
# category sections are dropped with a single "N categories not shown"
# context block.
SLACK_MAX_BLOCKS = 48

# Truncation bounds. 120 chars is what fits in a Slack section at a
# normal window width; 200 on bodies is enough to skim without eating
# the whole screen. X gets a wider title cap because X posts top out at
# 280 chars total — truncating mid-post would drop the entire payload
# when we could render it in full.
TITLE_MAX_CHARS = 120
TITLE_MAX_CHARS_X = 280
BODY_MAX_CHARS = 200

# Per-item body preview inside the digest. Lets you decide whether to
# click an HN comment ("Comment by nijave on HN #45809835") without
# opening it — the preview carries the lead of the comment body.
# Reddit self-posts, HN stories with self-text, and any other item
# with a non-empty body also show this. Link-only items (body empty)
# render with just the title line.
DIGEST_BODY_PREVIEW_MAX_CHARS = 200


def _title_max(source: str) -> int:
    return TITLE_MAX_CHARS_X if source == "x" else TITLE_MAX_CHARS


_FALLBACK_COLOR = "#555555"

_SOURCE_EMOJI: dict[str, str] = {
    "reddit": ":reddit:",
    "hackernews": ":hn:",
    # :twitter: rather than Slack's built-in :x: (which renders as a
    # red X mark / error glyph) — :twitter: is the custom workspace
    # emoji the user uploaded.
    "x": ":twitter:",
    "github": ":github:",
}


# --- input shapes ------------------------------------------------------------


@dataclass(frozen=True)
class NotifierConfig:
    """Project-scoped config the builders need.

    ``category_labels`` maps snake_case category ids to the human-
    friendly labels from ``categories.yaml``. Used in section headers
    and alert headers so the digest reads in prose rather than in
    snake_case ids.

    ``category_order`` is the project's declaration order from
    ``categories.yaml`` (highest priority first). The digest renders
    categories in this order and, when the Block Kit budget overflows,
    drops from the tail first — so whichever category the project
    declared last is the one sacrificed to keep real signal visible.
    An empty list falls back to alphabetical ordering; that's only
    exercised by unit tests of the pure builders that skip the
    project-config load.

    ``display_name`` is the full human-friendly header label used in
    the digest header. When set it replaces the entire ``Digest for
    <project>`` phrase — so an operator who configures ``display_name:
    "OpenData chatter"`` sees that string verbatim at the top of the
    digest, with no ``Digest for`` prefix. Unset falls back to the
    default ``Digest for <project>`` form.
    """

    project: str
    category_labels: dict[str, str] = field(default_factory=dict)
    category_order: list[str] = field(default_factory=list)
    display_name: str | None = None

    def category_display(self, category_id: str) -> str:
        """Return the human-friendly label for ``category_id``, or the id itself."""
        return self.category_labels.get(category_id, category_id)

    def header_label(self) -> str:
        """Return the Slack header label. ``display_name`` is treated as
        a complete label — when set it replaces the whole ``Digest for
        <project>`` phrase rather than just the project name. Unset
        falls back to the default ``Digest for <project>`` form."""
        return self.display_name or f"Digest for {self.project}"


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
    # True when this item was silenced within the digest window —
    # shown with 🔕 marker in its category. Older silences filter the
    # item out entirely upstream, so this flag is only ever true inside
    # the window.
    silenced: bool = False


@dataclass(frozen=True)
class XUsageSnapshot:
    """Authoritative X usage for the digest footer.

    Populated from :func:`sources.x.fetch_x_usage` at digest-build time.
    None means the usage API wasn't reachable or wasn't configured —
    footer falls back to a 'X usage unavailable' note.
    """

    project_usage: int
    project_cap: int
    cap_reset_day: int

    @property
    def percent(self) -> float:
        return (self.project_usage / self.project_cap * 100.0) if self.project_cap > 0 else 0.0


@dataclass(frozen=True)
class DigestStats:
    """Cost + accuracy footer for the digest.

    ``x_usage`` is the authoritative month-to-date consumption pulled
    from X's ``/2/usage/tweets`` endpoint. X doesn't expose a dollar
    figure via the API, so the footer shows posts-consumed rather than
    a locally-estimated cost.

    ``x_configured`` gates the footer's X segment. When False (no
    ``sources/x.yaml`` in the project) the segment is omitted entirely
    — saying "X usage unavailable" for a project that never used X
    would be noise, especially for forks that run only HN/Reddit. When
    True and ``x_usage`` is None, the footer renders a short "usage
    unavailable" note because we *expected* data and didn't get it.
    """

    day: date
    haiku_cost_usd: float
    total_labeled: int
    # Latest eval accuracy, or None if no eval has been run / recorded.
    accuracy_pct: float | None = None
    x_configured: bool = False
    x_usage: XUsageSnapshot | None = None


# --- builders ----------------------------------------------------------------


def build_immediate_alert(item: NotifierItem, config: NotifierConfig) -> dict[str, Any]:
    """Return the Block Kit payload for a single high-urgency item.

    Uses the attachment-with-color shape (not pure Block Kit) so the
    message picks up a category-colored left-edge bar. Everything
    inside the attachment is standard Block Kit.
    """
    color = CATEGORY_COLORS.get(item.category, _FALLBACK_COLOR)

    header = (
        f"{_source_label(item.source)}  *{config.category_display(item.category)}*  "
        f"·  urgency {item.urgency}"
    )
    title = _truncate(item.title or "(no title)", TITLE_MAX_CHARS)
    quote_lines: list[str] = [f"> {_linked_title(title, item.url)}"]
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

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": section_text},
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"Item ID: `{item.item_id}`"},
            ],
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

    Structure:

    1. Header line with day, counts, and cost
    2. One section per category in ``config.category_order`` (the
       project's declaration order from ``categories.yaml``), skipping
       empty categories. Each section shows the top 5 items by urgency
       (then recency), with an overflow hint if the category has more
       than 5.
    3. Cost/accuracy footer

    Order is entirely project-driven: whichever order the project
    declares its categories in ``categories.yaml`` is the order they
    render, top to bottom. When the Block Kit budget overflows,
    categories are dropped from the tail first — so operators who want
    a false-positive bucket like ``off_topic`` sacrificed before any
    real category simply declare it last.

    Items routed to the immediate channel do not appear here — they
    landed in the immediate Slack channel and are considered consumed.
    Items posted in a prior digest cycle are filtered out upstream in
    :func:`social_surveyor.cli_digest.run_digest` so each item ships in
    at most one digest.
    """
    by_category: dict[str, list[NotifierItem]] = {}
    for item in items:
        by_category.setdefault(item.category, []).append(item)

    # Project-declared order first; any category the classifier produced
    # but the project didn't declare (e.g. a post-rename drift) renders
    # after, alphabetically, so nothing silently disappears.
    declared_with_items = [c for c in config.category_order if by_category.get(c)]
    declared_set = set(config.category_order)
    undeclared = sorted(c for c in by_category if c not in declared_set)
    ordered_categories = [*declared_with_items, *undeclared]

    blocks: list[dict[str, Any]] = []

    # --- top header ---
    # Slack `header` blocks render noticeably larger than section text,
    # which gives the digest a clear visual top. Header blocks are
    # plain_text only (no mrkdwn), so we drop the `*bold*` wrapping —
    # header text is already visually bold on its own.
    #
    # Header used to carry a combined "$X today" figure. That was a
    # local estimate for X (posts times a hard-coded rate) and drifted
    # ~3x from what X actually billed. Cost detail lives in the footer
    # now, sourced from authoritative endpoints where available.
    if not items:
        top_header_text = (
            f"📊 {config.header_label()} · {stats.day.isoformat()} — no new items in the last 24h"
        )
    else:
        items_noun = "item" if len(items) == 1 else "items"
        cats_noun = "category" if len(ordered_categories) == 1 else "categories"
        top_header_text = (
            f"📊 {config.header_label()} · {stats.day.isoformat()} · "
            f"{len(items)} {items_noun} · {len(ordered_categories)} {cats_noun}"
        )
    blocks.append(_header_block(top_header_text))

    if not items:
        blocks.extend(_cost_footer(stats))
        return {"blocks": blocks}

    # --- per-category sections ---
    # Build each category's blocks as a self-contained group so the
    # budget trim at the end can drop whole categories cleanly.
    footer = _cost_footer(stats)
    # Reserve one block for a potential "N categories not shown" context.
    category_budget = SLACK_MAX_BLOCKS - len(blocks) - len(footer) - 1

    category_groups: list[tuple[str, list[dict[str, Any]]]] = []
    for cat in ordered_categories:
        category_groups.append((cat, _build_category_group(cat, by_category[cat], config)))

    # Tail-drop: whichever category the project ordered last is the
    # first to go when total blocks exceed the budget. Keeps the top
    # of the priority list intact and lets operators control what gets
    # sacrificed just by reordering categories.yaml.
    dropped_tail: list[str] = []
    total_needed = sum(len(group) for _, group in category_groups)
    while total_needed > category_budget and category_groups:
        cat, group = category_groups.pop()
        dropped_tail.append(cat)
        total_needed -= len(group)

    for _cat, group in category_groups:
        blocks.extend(group)

    # Surface drops in the same order they would have rendered.
    dropped_categories = list(reversed(dropped_tail))

    if dropped_categories:
        dropped_display = ", ".join(config.category_display(c) for c in dropped_categories)
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"_{len(dropped_categories)} categories not shown "
                            f"(digest block budget): {dropped_display}_"
                        ),
                    }
                ],
            }
        )

    blocks.extend(footer)
    return {"blocks": blocks}


def _build_category_group(
    cat: str,
    cat_items: list[NotifierItem],
    config: NotifierConfig,
) -> list[dict[str, Any]]:
    """Blocks for one category section: divider, header, up to ``TOP_N_PER_CATEGORY``
    item sections, optional overflow context.
    """
    # Sort by urgency desc, then by created_at desc (recency secondary).
    cat_items_sorted = sorted(
        cat_items,
        key=lambda i: (-(i.urgency), -i.created_at.timestamp()),
    )
    top = cat_items_sorted[:TOP_N_PER_CATEGORY]
    overflow = len(cat_items_sorted) - len(top)

    cat_display = config.category_display(cat)
    total_in_cat = len(cat_items_sorted)
    noun = "item" if total_in_cat == 1 else "items"
    if overflow > 0:
        header_line = f"{cat_display} · {total_in_cat} {noun} (top {len(top)} by urgency)"
    else:
        header_line = f"{cat_display} · {total_in_cat} {noun}"

    group: list[dict[str, Any]] = [{"type": "divider"}, _header_block(header_line)]
    for item in top:
        group.append(_digest_item_block(item, config))
    if overflow > 0:
        group.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"_{overflow} more {cat} items not shown_",
                    }
                ],
            }
        )
    return group


# --- block helpers -----------------------------------------------------------


def _digest_item_block(item: NotifierItem, config: NotifierConfig) -> dict[str, Any]:
    """One item inside a category section.

    Top line: source emoji, linked title, compact absolute timestamp,
    trailing monospace item-id. When the item has a body (HN comment,
    Reddit self-post, HN story with self-text), a second italic line
    carries a 200-char preview so you can decide whether to click
    without opening the item.

    No author, no flag prefix on the id — the id is the one piece of
    tooling metadata you'd copy, and Slack's code styling is enough to
    mark it as "this is the paste target."

    X-sourced items get a wider title cap (280 chars) because the
    entire X post fits there — truncating would drop signal we could
    otherwise show in full.
    """
    silenced_prefix = "🔕 " if item.silenced else ""
    title = _truncate(item.title or "(no title)", _title_max(item.source))
    first_line = (
        f"{silenced_prefix}{_source_label(item.source)}  "
        f"{_linked_title(title, item.url)}  ·  "
        f"{_digest_absolute_time(item.created_at)}  ·  `{item.item_id}`"
    )
    lines = [first_line]
    preview = _body_preview(item)
    if preview is not None:
        lines.append(f"_{preview}_")
    return {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}


def _digest_absolute_time(moment: datetime) -> str:
    """Short UTC timestamp for digest rows, e.g. ``"Apr 24 09:00Z"``.

    Items can be up to ``digest.max_item_age_hours`` old (7 days by
    default), so the month + day disambiguates yesterday-vs-last-week
    at a glance. The ``Z`` suffix pins the timezone — the digest
    process runs in UTC regardless of the project's schedule tz.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    utc = moment.astimezone(UTC)
    # %b is locale-dependent; in CI/prod both run under C.UTF-8 so this
    # renders "Apr" consistently. If we ever ship a non-English locale,
    # swap to the numeric form.
    return utc.strftime("%b %d %H:%MZ")


def _body_preview(item: NotifierItem) -> str | None:
    """Return a DIGEST_BODY_PREVIEW_MAX_CHARS-bounded preview of the item
    body, or ``None`` when there's nothing useful to show.

    Collapses internal whitespace (newlines, tabs) to single spaces so
    the preview renders as one line even when the source body is
    multi-paragraph. Skips the preview when body is empty or
    substantially identical to the title (Reddit self-posts
    occasionally duplicate title into selftext; showing both would
    waste a line).
    """
    body = (item.body or "").strip()
    if not body:
        return None
    title = (item.title or "").strip()
    if title and body.startswith(title):
        body = body[len(title) :].lstrip(" -—:")
    if not body:
        return None
    flat = " ".join(body.split())
    return _escape_mrkdwn(_truncate(flat, DIGEST_BODY_PREVIEW_MAX_CHARS))


def _linked_title(title: str, url: str | None) -> str:
    """Return a hyperlinked title in Slack mrkdwn, or plain text.

    No bold: Slack's link color gives the title enough visual weight
    without making the whole line shout. When no URL is available we
    return plain text — the line stays readable, it just doesn't
    click.
    """
    if not url:
        return title
    # Escape the angle-brackets inside the title to avoid breaking the
    # Slack link syntax. Pipe characters in titles do occur (rarely)
    # and would split the text from the URL; replace with a look-alike.
    safe = (
        title.replace("<", "\u2039")  # SINGLE LEFT-POINTING ANGLE QUOTATION MARK
        .replace(">", "\u203a")  # SINGLE RIGHT-POINTING ANGLE QUOTATION MARK
        .replace("|", "\u2758")  # LIGHT VERTICAL BAR
    )
    return f"<{url}|{safe}>"


def _header_block(text: str) -> dict[str, Any]:
    """Slack ``header`` block — bigger + bolder than ``section`` text.

    Plain-text only (no mrkdwn), 150-char cap. Unicode emoji render
    fine, but shortcodes need ``emoji: true``.
    """
    # Headers truncate at 150 chars; clamp defensively so a runaway
    # title in the header-text formatter doesn't 400 the payload.
    return {
        "type": "header",
        "text": {"type": "plain_text", "text": text[:150], "emoji": True},
    }


def _cost_footer(stats: DigestStats) -> list[dict[str, Any]]:
    """Tail of every digest: divider + one section with today's Haiku
    cost, X month-to-date usage pulled from X's own API, and labelling
    accuracy.

    X is shown as posts-consumed rather than dollars because X's
    ``/2/usage/tweets`` endpoint doesn't expose pricing — the dollar
    figure in the Developer Console is console-only. This is the
    authoritative number (not a local estimate), so operators can trust
    it. When the X API call fails, the footer degrades to a short
    'unavailable' note.
    """
    accuracy_bit = (
        f" · {stats.accuracy_pct:.1f}% accuracy" if stats.accuracy_pct is not None else ""
    )
    segments = [f"Today: ${stats.haiku_cost_usd:.2f} Haiku"]
    if stats.x_configured:
        if stats.x_usage is not None:
            segments.append(
                f"X month-to-date: {stats.x_usage.project_usage:,}/"
                f"{stats.x_usage.project_cap:,} posts "
                f"({stats.x_usage.percent:.1f}%, resets in "
                f"{stats.x_usage.cap_reset_day} days)"
            )
        else:
            segments.append("X usage unavailable (API unreachable)")
    # When X isn't configured for the project, omit the segment
    # entirely — a forked HN/Reddit-only deployment shouldn't see an
    # X footer line at all.
    segments.append(f"{stats.total_labeled} items labeled{accuracy_bit}")
    cost_text = "_" + " · ".join(segments) + "_"

    return [
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

    Link unfurling is force-disabled at post time (``unfurl_links`` and
    ``unfurl_media`` set to False). Our digests and alerts already carry
    titles, bodies, and IDs inline; Slack's preview cards duplicate that
    content and balloon the message height — a Reddit post alone renders
    a ~200px card under the digest footer. Setting both flags on every
    payload keeps the builders pure (they don't need to know about
    posting concerns) and covers callers that skip certain fields.
    """
    payload = {**payload, "unfurl_links": False, "unfurl_media": False}

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


# --- infra alerts ------------------------------------------------------------
# Ops-side messages (cost-cap halts, staleness, etc.) — visually
# distinct from content cards so the on-call can tell "your tool is
# unhappy" from "a cost-complaint post just appeared on HN".

_INFRA_SEVERITY_ICON: dict[str, str] = {
    "fatal": "🚨",
    "warn": "⚠️",
    "info": "ℹ️",  # noqa: RUF001 (INFORMATION SOURCE rendered as emoji, intentional)
}


@dataclass(frozen=True)
class InfraAlertChannel:
    """Resolved webhook target + optional subject prefix for an infra alert.

    ``source`` is a human-readable channel label used in structured
    logs (e.g. ``"infra"`` vs ``"immediate-fallback"``). ``prefix`` is
    prepended to the subject when posting into a shared channel so
    infra messages remain distinguishable from content alerts.
    """

    webhook_url: str
    source: str
    prefix: str = ""


def build_infra_alert(
    subject: str,
    body: str,
    *,
    severity: str = "fatal",
    prefix: str = "",
) -> dict[str, Any]:
    """Pure Block Kit payload for an infra alert.

    Kept deliberately simple: severity icon + subject + body, one
    section, no attachments. The information density of an immediate
    alert isn't warranted for ops messages, and staying off
    attachments means the message renders the same in every Slack
    client (mobile, desktop, mobile push preview).
    """
    icon = _INFRA_SEVERITY_ICON.get(severity, _INFRA_SEVERITY_ICON["fatal"])
    full_subject = f"{prefix}{subject}" if prefix else subject
    text = (
        f"{icon}  *{severity.upper()}*  ·  {_escape_mrkdwn(full_subject)}\n{_escape_mrkdwn(body)}"
    )
    return {
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        ]
    }


def post_infra_alert(
    channel: InfraAlertChannel,
    *,
    subject: str,
    body: str,
    severity: str = "fatal",
    client: httpx.Client | None = None,
) -> None:
    """Build and POST an infra alert. Raises :class:`SlackPostError` on
    a non-200 response.

    ``client`` is injectable for tests via ``httpx.MockTransport``. The
    caller decides whether to swallow failures — for cost-cap halts the
    caller logs and continues so a flaky Slack doesn't mask the halt
    itself.
    """
    payload = build_infra_alert(subject, body, severity=severity, prefix=channel.prefix)
    post_to_slack(payload, channel.webhook_url, client=client)


__all__ = [
    "CATEGORY_COLORS",
    "SLACK_MAX_BLOCKS",
    "TOP_N_PER_CATEGORY",
    "DigestStats",
    "InfraAlertChannel",
    "NotifierConfig",
    "NotifierItem",
    "SlackPostError",
    "XUsageSnapshot",
    "build_digest",
    "build_immediate_alert",
    "build_infra_alert",
    "post_infra_alert",
    "post_to_slack",
]
