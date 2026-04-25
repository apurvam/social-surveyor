from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest

from social_surveyor.notifier import (
    CATEGORY_COLORS,
    SLACK_MAX_BLOCKS,
    TOP_N_PER_CATEGORY,
    DigestStats,
    InfraAlertChannel,
    NotifierConfig,
    NotifierItem,
    SlackPostError,
    build_digest,
    build_immediate_alert,
    build_infra_alert,
    post_infra_alert,
    post_to_slack,
)

# Canonical opendata taxonomy — used as the default for tests that
# predated the move to project-driven ordering. New tests that care
# about a specific taxonomy should pass `category_order` explicitly
# via `_cfg(category_order=[...])`.
_DEFAULT_TEST_CATEGORY_ORDER: list[str] = [
    "cost_complaint",
    "self_host_intent",
    "competitor_pain",
    "active_practitioner",
    "neutral_discussion",
    "tutorial_or_marketing",
    "off_topic",
]


def _cfg(
    project: str = "opendata",
    *,
    category_order: list[str] | None = None,
    category_labels: dict[str, str] | None = None,
    display_name: str | None = None,
) -> NotifierConfig:
    return NotifierConfig(
        project=project,
        category_labels=category_labels or {},
        category_order=list(
            category_order if category_order is not None else _DEFAULT_TEST_CATEGORY_ORDER
        ),
        display_name=display_name,
    )


def _item(
    *,
    item_id: str = "hackernews:42",
    source: str = "hackernews",
    category: str = "cost_complaint",
    urgency: int = 8,
    title: str = "Datadog costs doubled overnight",
    body: str | None = "Was paying $30k/mo, now it's $80k/mo with the same volume.",
    author: str | None = "ops-lead",
    url: str | None = "https://news.ycombinator.com/item?id=42",
    created_at: datetime | None = None,
    reasoning: str | None = "Explicit first-person cost pain with concrete numbers.",
    silenced: bool = False,
) -> NotifierItem:
    return NotifierItem(
        item_id=item_id,
        source=source,
        category=category,
        urgency=urgency,
        title=title,
        body=body,
        author=author,
        url=url,
        created_at=created_at or datetime(2026, 4, 19, 12, 0, tzinfo=UTC),
        reasoning=reasoning,
        silenced=silenced,
    )


def _all_text(blocks: list[dict[str, Any]]) -> str:
    """Flatten every mrkdwn text in a block list for easy substring search."""
    parts: list[str] = []
    for b in blocks:
        t = b.get("text")
        if isinstance(t, dict) and "text" in t:
            parts.append(t["text"])
        for el in b.get("elements") or []:
            if isinstance(el, dict) and "text" in el:
                parts.append(el["text"])
    return "\n".join(parts)


# --- immediate alert ---------------------------------------------------------


def test_immediate_alert_uses_category_color_attachment() -> None:
    payload = build_immediate_alert(_item(category="cost_complaint"), _cfg())
    assert "attachments" in payload
    assert len(payload["attachments"]) == 1
    att = payload["attachments"][0]
    assert att["color"] == CATEGORY_COLORS["cost_complaint"]
    assert isinstance(att["blocks"], list) and att["blocks"]


def test_immediate_alert_contains_title_urgency_and_item_id() -> None:
    payload = build_immediate_alert(_item(), _cfg(project="opendata"))
    text = _all_text(payload["attachments"][0]["blocks"])
    assert "cost_complaint" in text
    assert "urgency 8" in text
    assert "Datadog costs doubled overnight" in text
    # Item id shows in its own context block for easy copying.
    assert "hackernews:42" in text


def test_immediate_alert_does_not_include_cli_copy_paste_commands() -> None:
    """CLI copy-paste lines were removed — a coding agent constructs
    corrections from the item id + intent; the static text was noise."""
    payload = build_immediate_alert(_item(), _cfg(project="opendata"))
    text = _all_text(payload["attachments"][0]["blocks"])
    for needle in ("sv label", "sv silence", "sv ingest", "social-surveyor label"):
        assert needle not in text
    # Item id still present — that's the piece a coding agent actually needs.
    assert "hackernews:42" in text


def test_immediate_alert_handles_missing_optional_fields() -> None:
    """No title / no body / no author / no URL shouldn't crash; builder
    substitutes sensible defaults."""
    payload = build_immediate_alert(
        _item(title="", body=None, author=None, url=None, reasoning=None),
        _cfg(),
    )
    text = _all_text(payload["attachments"][0]["blocks"])
    assert "(no title)" in text
    assert "unknown" in text
    # No "Open in <source>" link when url is missing.
    assert "Open in" not in text


def test_immediate_alert_truncates_long_title_and_body() -> None:
    long_title = "A" * 500
    long_body = "B" * 500
    payload = build_immediate_alert(_item(title=long_title, body=long_body), _cfg())
    text = _all_text(payload["attachments"][0]["blocks"])
    # Neither blob should appear at full length; both get an ellipsis.
    assert "A" * 500 not in text
    assert "B" * 500 not in text
    assert "…" in text


def test_immediate_alert_defensive_color_for_unknown_category() -> None:
    """An unknown category (fork with custom taxonomy) gets a fallback
    color rather than raising."""
    payload = build_immediate_alert(_item(category="novel_category"), _cfg())
    assert payload["attachments"][0]["color"] == "#555555"


# --- digest ------------------------------------------------------------------


def test_digest_empty_day_renders_payload_for_dry_run_inspection() -> None:
    """Zero items still returns a valid payload for `--dry-run` inspection,
    even though :func:`run_digest` now short-circuits the live post.
    Header carries the project name + date so the empty output is
    still unambiguous when the operator eyeballs the JSON."""
    payload = build_digest(
        [],
        DigestStats(
            day=date(2026, 4, 19),
            haiku_cost_usd=0.0,
            total_labeled=143,
            accuracy_pct=61.5,
        ),
        _cfg(project="opendata"),
    )
    text = _all_text(payload["blocks"])
    assert "Digest for opendata · 2026-04-19" in text
    assert "no new items in the last 24h" in text


def test_digest_header_includes_project_name() -> None:
    """Top header names the project so a shared Slack channel can tell
    two projects' digests apart at a glance without digging into the
    body."""
    items = [_item(item_id="hackernews:1", category="cost_complaint", urgency=7)]
    payload = build_digest(
        items,
        DigestStats(
            day=date(2026, 4, 19),
            haiku_cost_usd=0.12,
            total_labeled=0,
        ),
        _cfg(project="opendata-brand"),
    )
    headers = [b["text"]["text"] for b in payload["blocks"] if b.get("type") == "header"]
    assert "Digest for opendata-brand · 2026-04-19" in headers[0]


def test_digest_header_uses_display_name_when_set() -> None:
    """When the project sets ``digest.display_name`` in routing.yaml,
    the label replaces the full ``Digest for <project>`` phrase — so
    "opendata-brand" reads as "OpenData chatter" in Slack with no
    ``Digest for`` prefix dangling in front.
    """
    items = [_item(item_id="hackernews:1", category="cost_complaint", urgency=7)]
    payload = build_digest(
        items,
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0.12, total_labeled=0),
        _cfg(project="opendata-brand", display_name="OpenData chatter"),
    )
    headers = [b["text"]["text"] for b in payload["blocks"] if b.get("type") == "header"]
    assert "📊 OpenData chatter · 2026-04-19" in headers[0]
    # display_name replaces the whole "Digest for ..." phrase — the
    # prefix should not appear anywhere in the header when it's set.
    assert "Digest for" not in headers[0]
    # The on-disk id should not leak into the header when display_name is set.
    assert "opendata-brand" not in headers[0]


def test_digest_header_falls_back_to_project_when_display_name_unset() -> None:
    """Unset display_name → header shows the project directory name,
    preserving the pre-feature behavior and keeping a migration-free
    default for projects that haven't opted in."""
    items = [_item(item_id="hackernews:1", category="cost_complaint", urgency=7)]
    payload = build_digest(
        items,
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0.12, total_labeled=0),
        _cfg(project="opendata", display_name=None),
    )
    headers = [b["text"]["text"] for b in payload["blocks"] if b.get("type") == "header"]
    assert "Digest for opendata · 2026-04-19" in headers[0]


def test_digest_empty_day_header_also_uses_display_name() -> None:
    """The "no new items" variant of the header must honor display_name
    too — otherwise on a quiet day the header would revert to the
    on-disk id (and re-introduce the ``Digest for`` prefix that
    display_name is meant to replace)."""
    payload = build_digest(
        [],
        DigestStats(day=date(2026, 4, 24), haiku_cost_usd=0.0, total_labeled=0),
        _cfg(project="opendata-brand", display_name="OpenData chatter"),
    )
    text = _all_text(payload["blocks"])
    assert "📊 OpenData chatter · 2026-04-24" in text
    assert "Digest for" not in text
    assert "opendata-brand" not in text


def test_digest_renders_categories_in_fixed_order() -> None:
    """Ordering is category-first, alert-worthy before the rest — holds
    regardless of which order items come in."""
    items = [
        _item(item_id="x:1", category="off_topic", urgency=0, title="off topic item"),
        _item(
            item_id="hackernews:2",
            category="self_host_intent",
            urgency=7,
            title="self-host intent item",
        ),
        _item(
            item_id="reddit:3",
            source="reddit",
            category="cost_complaint",
            urgency=8,
            title="cost complaint item",
        ),
    ]
    payload = build_digest(
        items,
        DigestStats(
            day=date(2026, 4, 19),
            haiku_cost_usd=0.12,
            total_labeled=143,
        ),
        _cfg(),
    )
    text = _all_text(payload["blocks"])
    cc = text.index("cost complaint item")
    sh = text.index("self-host intent item")
    ot = text.index("off topic item")
    assert cc < sh < ot


def test_digest_top_n_caps_category_and_shows_overflow_hint() -> None:
    """Categories with more than TOP_N items show only the top N plus
    a hint pointing at the CLI inspection command."""
    base = datetime(2026, 4, 19, 6, 0, tzinfo=UTC)
    items = [
        _item(
            item_id=f"hackernews:{i}",
            category="active_practitioner",
            urgency=10 - i,  # distinct urgencies for stable top-N
            title=f"active item {i}",
            created_at=base + timedelta(minutes=i),
        )
        for i in range(TOP_N_PER_CATEGORY + 3)  # 8 items, should show top 5 + 3 overflow
    ]
    payload = build_digest(
        items,
        DigestStats(
            day=date(2026, 4, 19),
            haiku_cost_usd=0.0,
            total_labeled=0,
        ),
        _cfg(project="opendata"),
    )
    text = _all_text(payload["blocks"])
    # Header line reflects total count + "showing top N".
    assert "active_practitioner" in text
    assert f"{len(items)} items" in text
    assert f"top {TOP_N_PER_CATEGORY} by urgency" in text
    # Top-urgency item present, lowest-urgency item not.
    assert "active item 0" in text  # urgency 10
    assert "active item 7" not in text  # urgency 3, below the top-5 cut
    # Overflow hint names the leftover count; CLI pointer was removed.
    assert "3 more active_practitioner items" in text


def test_digest_no_overflow_hint_when_under_cap() -> None:
    items = [
        _item(item_id=f"hackernews:{i}", category="cost_complaint", urgency=8, title=f"t{i}")
        for i in range(3)
    ]
    payload = build_digest(
        items,
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, total_labeled=0),
        _cfg(),
    )
    text = _all_text(payload["blocks"])
    assert "more cost_complaint items" not in text
    assert "showing top" not in text


def test_digest_has_no_alerted_earlier_section() -> None:
    """The digest renders only items pending in the digest channel.
    Immediate-channel alerts already landed in their own Slack channel
    and are considered consumed — no recap section, no "N alerted" in
    the top header.
    """
    items = [
        _item(
            item_id="hackernews:100",
            title="routine cost complaint",
            category="cost_complaint",
            urgency=7,
        ),
    ]
    payload = build_digest(
        items,
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, total_labeled=0),
        _cfg(),
    )
    text = _all_text(payload["blocks"])
    assert "Alerted earlier today" not in text
    assert "alerted at" not in text
    # Header no longer carries an "alerted" count.
    headers = [b["text"]["text"] for b in payload["blocks"] if b.get("type") == "header"]
    assert not any("alerted" in h for h in headers)


def test_digest_silenced_within_window_shows_marker() -> None:
    silenced = _item(
        item_id="hackernews:200",
        title="noisy tutorial",
        category="tutorial_or_marketing",
        urgency=4,
        silenced=True,
    )
    payload = build_digest(
        [silenced],
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, total_labeled=0),
        _cfg(),
    )
    text = _all_text(payload["blocks"])
    assert "🔕" in text
    assert "noisy tutorial" in text


def test_digest_cost_footer_includes_accuracy() -> None:
    from social_surveyor.notifier import XUsageSnapshot

    payload = build_digest(
        [_item()],
        DigestStats(
            day=date(2026, 4, 19),
            haiku_cost_usd=0.12,
            total_labeled=143,
            accuracy_pct=61.5,
            x_configured=True,
            x_usage=XUsageSnapshot(project_usage=143, project_cap=10_000, cap_reset_day=21),
        ),
        _cfg(project="opendata"),
    )
    text = _all_text(payload["blocks"])
    assert "$0.12 Haiku" in text
    # X now renders as authoritative posts-consumed, not a $ figure.
    assert "143/10,000 posts" in text
    assert "resets in 21 days" in text
    assert "143 items labeled" in text
    assert "61.5% accuracy" in text


def test_digest_cost_footer_degrades_when_x_usage_unavailable() -> None:
    """When X is configured but /2/usage/tweets can't be reached, the
    footer shows a short 'unavailable' note rather than a made-up
    number."""
    payload = build_digest(
        [_item()],
        DigestStats(
            day=date(2026, 4, 19),
            haiku_cost_usd=0.12,
            total_labeled=143,
            x_configured=True,
            x_usage=None,
        ),
        _cfg(project="opendata"),
    )
    text = _all_text(payload["blocks"])
    assert "X usage unavailable" in text
    # Haiku cost still shown — degradation is X-side only.
    assert "$0.12 Haiku" in text


def test_digest_cost_footer_omits_x_segment_when_not_configured() -> None:
    """A forked HN/Reddit-only project with no sources/x.yaml: the
    footer should not mention X at all — neither 'usage unavailable'
    nor a usage line. Clean output for deployments that never touched X.
    """
    payload = build_digest(
        [_item()],
        DigestStats(
            day=date(2026, 4, 19),
            haiku_cost_usd=0.12,
            total_labeled=143,
            x_configured=False,  # explicit: X not in this project
            x_usage=None,
        ),
        _cfg(project="opendata"),
    )
    text = _all_text(payload["blocks"])
    # Footer carries the Haiku line and the labeled count, nothing about X.
    assert "$0.12 Haiku" in text
    assert "143 items labeled" in text
    assert "X usage" not in text
    assert "X month-to-date" not in text
    # Specifically the word "X " doesn't slip into the footer line.
    footer_line = next(
        b["text"]["text"]
        for b in payload["blocks"]
        if b.get("type") == "section" and "items labeled" in b.get("text", {}).get("text", "")
    )
    assert " X " not in footer_line


def test_digest_does_not_include_cli_copy_paste_commands() -> None:
    """The digest no longer carries the per-category CLI inspection
    pointer, the category listing, or the correction footer block."""
    payload = build_digest(
        [_item()],
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, total_labeled=1),
        _cfg(project="opendata"),
    )
    text = _all_text(payload["blocks"])
    for needle in (
        "sv label",
        "sv silence",
        "sv ingest",
        "sv digest --project",
        "social-surveyor label",
        "Correct a classification",
    ):
        assert needle not in text


def test_digest_uses_header_blocks_for_hierarchy() -> None:
    """Top-level digest header + per-category headers + correction
    header must all be Block Kit `header` blocks so Slack renders them
    larger/bolder than the per-item section text."""
    payload = build_digest(
        [
            _item(category="cost_complaint", item_id="x:1", title="A"),
            _item(category="off_topic", item_id="x:2", title="B", urgency=0),
        ],
        DigestStats(
            day=date(2026, 4, 19),
            haiku_cost_usd=0.0,
            total_labeled=0,
        ),
        _cfg(),
    )
    headers = [b["text"]["text"] for b in payload["blocks"] if b.get("type") == "header"]
    # Top header first.
    assert headers[0].startswith("📊 Digest for ")
    # Per-category headers for both populated categories.
    assert any("cost_complaint" in h for h in headers)
    assert any("off_topic" in h for h in headers)


def test_digest_item_title_is_hyperlinked_to_url() -> None:
    """Every digest item should be one click from the discussion."""
    item = _item(
        item_id="hackernews:42",
        title="Datadog costs doubled",
        url="https://news.ycombinator.com/item?id=42",
        category="cost_complaint",
    )
    payload = build_digest(
        [item],
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, total_labeled=0),
        _cfg(),
    )
    text = _all_text(payload["blocks"])
    # Plain Slack link mrkdwn — no surrounding bold so the line reads
    # like a news-feed entry rather than a dashboard.
    assert "<https://news.ycombinator.com/item?id=42|Datadog costs doubled>" in text
    # And we're not silently re-adding bold wrappers.
    assert "*<https" not in text


def test_digest_item_line_includes_absolute_timestamp_between_title_and_id() -> None:
    """Each digest row renders as:
    ``<emoji>  <title-link>  ·  <compact UTC timestamp>  ·  <item-id>``.

    The timestamp is UTC, month-name-day-HH:MM with a trailing Z so an
    operator can tell "2h ago" from "6 days ago" without opening the
    link. Position matters: timestamp sits between the link and the id
    so the id stays the rightmost copy target.
    """
    item = _item(
        item_id="hackernews:42",
        title="Datadog costs doubled",
        url="https://news.ycombinator.com/item?id=42",
        category="cost_complaint",
        created_at=datetime(2026, 4, 24, 9, 5, tzinfo=UTC),
    )
    payload = build_digest(
        [item],
        DigestStats(day=date(2026, 4, 24), haiku_cost_usd=0, total_labeled=0),
        _cfg(),
    )
    text = _all_text(payload["blocks"])
    # Exact compact form, with the Z suffix.
    assert "Apr 24 09:05Z" in text
    # Appears between the link and the item-id on the same line.
    title_end = text.index("Datadog costs doubled>") + len("Datadog costs doubled>")
    ts_pos = text.index("Apr 24 09:05Z")
    id_pos = text.index("`hackernews:42`")
    assert title_end < ts_pos < id_pos
    # And they're all on the same line (no newline between title and id).
    between = text[title_end:id_pos]
    assert "\n" not in between


def test_digest_item_timestamp_renders_naive_datetime_as_utc() -> None:
    """Defensive: if a NotifierItem ever arrives with a tz-naive
    ``created_at`` (e.g. from a test fixture or legacy row that didn't
    round-trip through _from_iso), the timestamp still renders — as UTC
    — rather than raising.
    """
    item = _item(created_at=datetime(2026, 4, 24, 9, 5))  # no tzinfo
    payload = build_digest(
        [item],
        DigestStats(day=date(2026, 4, 24), haiku_cost_usd=0, total_labeled=0),
        _cfg(),
    )
    assert "Apr 24 09:05Z" in _all_text(payload["blocks"])


def test_digest_item_without_url_falls_back_to_plain_title() -> None:
    """Items without a URL render as plain text — no stray link syntax
    and no bold wrapper that would lie about clickability."""
    item = _item(url=None, title="No link available")
    payload = build_digest(
        [item],
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, total_labeled=0),
        _cfg(),
    )
    text = _all_text(payload["blocks"])
    assert "No link available" in text
    # No Slack link syntax around the title.
    around = text.split("No link available")[0][-10:] + text.split("No link available")[1][:10]
    assert "<http" not in around and "<|" not in around


def test_digest_item_title_escapes_pipe_and_angle_brackets() -> None:
    """A pipe in the title would break Slack's <url|text> syntax;
    angle brackets would too. Both are replaced with look-alikes."""
    item = _item(
        title="What's the deal with <script>|pipe?",
        url="https://example.com/x",
    )
    payload = build_digest(
        [item],
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, total_labeled=0),
        _cfg(),
    )
    text = _all_text(payload["blocks"])
    # Pipe replaced with look-alike; angle brackets too.
    assert "\u2758" in text  # LIGHT VERTICAL BAR (pipe look-alike)
    assert "\u2039script\u203a" in text  # angle-quote wrapped


def test_digest_items_include_bare_item_id_subtext() -> None:
    """Every digest item ends in a monospace ``<item_id>`` span.

    Just the id, no ``--item-id`` prefix — the code styling is enough
    to mark it as "this is the paste target." The flag name is noise
    when every digest line has the same one.
    """
    item = _item(
        item_id="hackernews:42",
        category="cost_complaint",
        urgency=8,
    )
    payload = build_digest(
        [item],
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, total_labeled=0),
        _cfg(),
    )
    text = _all_text(payload["blocks"])
    assert "`hackernews:42`" in text
    # Per-line subtexts no longer carry the --item-id flag.
    assert "`--item-id hackernews:42`" not in text
    # Category / urgency still excluded from per-line subtexts.
    pre_footer = text.split("Correct a classification")[0]
    assert "--category cost_complaint" not in pre_footer


def test_digest_x_items_allow_up_to_280_chars() -> None:
    """X posts top out at 280 chars. Truncating at 120 would drop the
    payload; give X a wider cap."""
    long_post = "A" * 280
    item = _item(item_id="x:1", source="x", title=long_post, category="neutral_discussion")
    payload = build_digest(
        [item],
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, total_labeled=0),
        _cfg(),
    )
    text = _all_text(payload["blocks"])
    assert "A" * 280 in text
    # And we don't silently extend the cap — 281 still truncates.
    too_long = _item(item_id="x:2", source="x", title="B" * 300, category="neutral_discussion")
    payload2 = build_digest(
        [too_long],
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, total_labeled=0),
        _cfg(),
    )
    text2 = _all_text(payload2["blocks"])
    assert "B" * 300 not in text2
    assert "…" in text2


def test_digest_item_includes_body_preview_when_body_present() -> None:
    """HN comments (and any other item with a non-empty body) get a
    200-char italic preview line under the linked title. Lets a reader
    decide whether to click without opening the item."""
    item = _item(
        title="Comment by nijave on HN #45809835",
        body="Datadog's pricing made sense when we were 1/10 the size. At current volume it's the single biggest line item.",
    )
    payload = build_digest(
        [item],
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, total_labeled=0),
        _cfg(),
    )
    text = _all_text(payload["blocks"])
    assert "Datadog's pricing made sense when we were 1/10 the size" in text


def test_digest_item_body_preview_truncated_for_long_body() -> None:
    long_body = "A" * 500
    item = _item(title="some title", body=long_body)
    payload = build_digest(
        [item],
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, total_labeled=0),
        _cfg(),
    )
    text = _all_text(payload["blocks"])
    assert long_body not in text
    assert "…" in text


def test_digest_item_body_preview_collapses_internal_whitespace() -> None:
    """Multi-paragraph bodies render as one flat preview line so the
    card stays compact."""
    item = _item(
        title="some title",
        body="First paragraph.\n\nSecond paragraph\nwith a wrapped line.",
    )
    payload = build_digest(
        [item],
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, total_labeled=0),
        _cfg(),
    )
    text = _all_text(payload["blocks"])
    assert "First paragraph. Second paragraph with a wrapped line." in text


def test_digest_item_without_body_has_no_preview_line() -> None:
    """Link-only posts (body empty) keep the single-line layout — no
    empty italic stub."""
    item = _item(title="A link-only story", body=None)
    payload = build_digest(
        [item],
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, total_labeled=0),
        _cfg(),
    )
    # Find the item block and confirm it's one line only (no trailing
    # italic preview line).
    item_sections = [
        b
        for b in payload["blocks"]
        if b.get("type") == "section"
        and isinstance(b.get("text"), dict)
        and "A link-only story" in b["text"].get("text", "")
    ]
    assert len(item_sections) == 1
    assert "\n" not in item_sections[0]["text"]["text"]


def test_digest_items_do_not_include_author() -> None:
    """Author is dropped from per-item lines (low signal; title +
    source do the work)."""
    item = _item(title="some title", author="the-author")
    payload = build_digest(
        [item],
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, total_labeled=0),
        _cfg(),
    )
    text = _all_text(payload["blocks"])
    assert "the-author" not in text


def test_digest_uses_human_category_labels_when_available() -> None:
    """When category_labels is populated, section headers and the
    alerted-earlier block show the human-friendly label, not the
    snake_case id."""
    cfg = NotifierConfig(
        project="opendata",
        category_labels={
            "cost_complaint": "Observability cost complaint",
            "self_host_intent": "Self-host Prometheus intent",
        },
    )
    items = [
        _item(item_id="reddit:1", category="cost_complaint", title="a", urgency=7),
        _item(item_id="reddit:2", category="cost_complaint", title="b", urgency=8),
    ]
    payload = build_digest(
        items,
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, total_labeled=0),
        cfg,
    )
    text = _all_text(payload["blocks"])
    # Section header uses the label.
    assert "Observability cost complaint" in text
    # The snake_case id doesn't appear in any per-item subtext — those
    # carry only the item id now.
    assert "--category cost_complaint" not in text


def test_digest_category_header_singular_vs_plural() -> None:
    one_item = _item(item_id="x:1", category="cost_complaint")
    two_items = [
        _item(item_id="x:2", category="cost_complaint", title="a"),
        _item(item_id="x:3", category="cost_complaint", title="b"),
    ]
    single = _all_text(
        build_digest(
            [one_item],
            DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, total_labeled=0),
            _cfg(),
        )["blocks"]
    )
    plural = _all_text(
        build_digest(
            two_items,
            DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, total_labeled=0),
            _cfg(),
        )["blocks"]
    )
    assert "· 1 item" in single and "· 1 items" not in single
    assert "· 2 items" in plural


# --- digest block-cap guards -------------------------------------------------


def test_digest_total_blocks_stays_under_slack_limit_on_worst_case() -> None:
    """Burst day: many categories each with many items. The naive builder
    would blow past 50 blocks; the budget trim keeps it safely under
    SLACK_MAX_BLOCKS.
    """
    cats = [
        "cost_complaint",
        "self_host_intent",
        "competitor_pain",
        "active_practitioner",
        "migration_friction",
        "tutorial_or_marketing",
        "off_topic",
    ]
    items: list[NotifierItem] = []
    for c in cats:
        for i in range(40):
            items.append(
                _item(
                    item_id=f"hackernews:{c}-{i}",
                    category=c,
                    urgency=5,
                    title=f"{c} item {i}",
                )
            )
    payload = build_digest(
        items,
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, total_labeled=0),
        _cfg(),
    )
    assert len(payload["blocks"]) <= SLACK_MAX_BLOCKS


def test_digest_dropped_categories_get_context_notice() -> None:
    """When categories can't fit the budget, the trailing ones (last in
    ``config.category_order``) are dropped with a single notice."""
    cats = [
        "cost_complaint",
        "self_host_intent",
        "competitor_pain",
        "active_practitioner",
        "migration_friction",
        "tutorial_or_marketing",
        "off_topic",
    ]
    items: list[NotifierItem] = []
    for c in cats:
        for i in range(10):
            items.append(
                _item(
                    item_id=f"hackernews:{c}-{i}",
                    category=c,
                    urgency=5,
                )
            )
    payload = build_digest(
        items,
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, total_labeled=0),
        _cfg(),
    )
    text = _all_text(payload["blocks"])
    # At least one category dropped.
    assert "categories not shown" in text
    # Priority-ordered: cost_complaint (highest priority) must be present.
    assert "Observability cost complaint" in text or "cost_complaint" in text


def test_digest_light_day_no_budget_trim() -> None:
    """A normal day easily fits — no "not shown" notice."""
    items = [
        _item(item_id="hackernews:1", category="cost_complaint", urgency=7),
        _item(item_id="hackernews:2", category="cost_complaint", urgency=9),
        _item(item_id="reddit:1", category="self_host_intent", urgency=6),
    ]
    payload = build_digest(
        items,
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0.1, total_labeled=3),
        _cfg(),
    )
    text = _all_text(payload["blocks"])
    assert "categories not shown" not in text
    assert len(payload["blocks"]) <= SLACK_MAX_BLOCKS


def test_digest_renders_categories_in_project_declared_order() -> None:
    """A fork's categories.yaml declaration order drives section order.
    Whatever the project declared first renders first, regardless of
    alphabet or how items arrived.
    """
    # Fork taxonomy (opendata-brand shape): note off_topic is declared
    # last, and the other categories are NOT in alphabetical order —
    # this tests that the project's explicit ordering wins.
    fork_order = [
        "direct_question",
        "issue_or_complaint",
        "comparison",
        "off_topic",
    ]
    items = [
        _item(item_id="x:1", category="off_topic", urgency=5, title="off-topic item"),
        _item(item_id="x:2", category="comparison", urgency=5, title="comparison item"),
        _item(
            item_id="x:3",
            category="direct_question",
            urgency=5,
            title="direct-question item",
        ),
        _item(
            item_id="x:4",
            category="issue_or_complaint",
            urgency=5,
            title="issue-or-complaint item",
        ),
    ]
    payload = build_digest(
        items,
        DigestStats(day=date(2026, 4, 24), haiku_cost_usd=0.0, total_labeled=0),
        _cfg(category_order=fork_order),
    )
    text = _all_text(payload["blocks"])
    positions = [text.index(f"{label} item") for label in fork_order_labels(fork_order)]
    assert positions == sorted(positions)


def fork_order_labels(order: list[str]) -> list[str]:
    # Titles use hyphenated forms; map ids to matching search terms.
    mapping = {
        "direct_question": "direct-question",
        "issue_or_complaint": "issue-or-complaint",
        "comparison": "comparison",
        "off_topic": "off-topic",
    }
    return [mapping[c] for c in order]


def test_digest_renders_undeclared_categories_alphabetically_at_tail() -> None:
    """If the classifier produces a category the project didn't declare
    (post-rename drift, typo), it still renders — at the tail, in
    alphabetical order — so no signal is silently dropped.
    """
    declared = ["cost_complaint"]
    items = [
        _item(item_id="hackernews:1", category="cost_complaint", urgency=8, title="declared item"),
        _item(item_id="hackernews:2", category="zeta_leftover", urgency=5, title="zeta item"),
        _item(item_id="hackernews:3", category="alpha_drift", urgency=5, title="alpha item"),
    ]
    payload = build_digest(
        items,
        DigestStats(day=date(2026, 4, 24), haiku_cost_usd=0.0, total_labeled=0),
        _cfg(category_order=declared),
    )
    text = _all_text(payload["blocks"])
    declared_pos = text.index("declared item")
    alpha_pos = text.index("alpha item")
    zeta_pos = text.index("zeta item")
    assert declared_pos < alpha_pos < zeta_pos


def test_digest_drops_last_category_first_when_over_budget() -> None:
    """Over-budget trimming drops from the tail of ``category_order``.
    Operators who want a specific category sacrificed first (e.g. a
    false-positive bucket like off_topic) declare it last in
    categories.yaml — no hard-coded special case.
    """
    order = [
        "cost_complaint",
        "self_host_intent",
        "competitor_pain",
        "active_practitioner",
        "neutral_discussion",
        "tutorial_or_marketing",
        "migration_friction",
        "off_topic",
    ]
    items: list[NotifierItem] = []
    for c in order:
        for i in range(6):
            items.append(
                _item(
                    item_id=f"hackernews:{c}-{i}",
                    category=c,
                    urgency=5,
                    title=f"{c} item {i}",
                )
            )
    payload = build_digest(
        items,
        DigestStats(day=date(2026, 4, 24), haiku_cost_usd=0.0, total_labeled=0),
        _cfg(category_order=order),
    )
    text = _all_text(payload["blocks"])
    assert "categories not shown" in text
    assert "off_topic" in text  # named in the dropped notice
    # No off_topic body rendered.
    for i in range(6):
        assert f"off_topic item {i}" not in text


def test_digest_fork_tail_drops_first_when_over_budget() -> None:
    """Tail-drop is fork-aware: the category a fork declared last is
    the one dropped, even if it isn't ``off_topic``. Proves the drop
    behavior follows project order rather than any built-in list.
    """
    # Fork taxonomy where "neutral_mention" is declared last. Enough
    # items per category to force the budget to overflow.
    order = [
        "direct_question",
        "issue_or_complaint",
        "comparison",
        "positive_mention",
        "ecosystem_discussion",
        "off_topic",
        "neutral_mention",
    ]
    items: list[NotifierItem] = []
    for c in order:
        for i in range(6):
            items.append(
                _item(
                    item_id=f"reddit:{c}-{i}",
                    category=c,
                    urgency=5,
                    title=f"{c} item {i}",
                )
            )
    payload = build_digest(
        items,
        DigestStats(day=date(2026, 4, 24), haiku_cost_usd=0.0, total_labeled=0),
        _cfg(category_order=order),
    )
    text = _all_text(payload["blocks"])
    assert "categories not shown" in text
    # neutral_mention was last in the fork's declared order → it's the
    # first to be sacrificed, even though off_topic exists earlier.
    for i in range(6):
        assert f"neutral_mention item {i}" not in text


def test_digest_keeps_off_topic_when_budget_comfortable() -> None:
    """With plenty of room, off_topic still renders (at the tail)."""
    items = [
        _item(item_id="hackernews:1", category="cost_complaint", urgency=7),
        _item(
            item_id="hackernews:2",
            category="off_topic",
            urgency=0,
            title="benign off topic",
        ),
    ]
    payload = build_digest(
        items,
        DigestStats(day=date(2026, 4, 24), haiku_cost_usd=0.0, total_labeled=0),
        _cfg(),
    )
    text = _all_text(payload["blocks"])
    assert "benign off topic" in text
    assert "categories not shown" not in text


# --- post_to_slack -----------------------------------------------------------


def test_post_to_slack_sends_payload_and_succeeds_on_200() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(200, text="ok")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        post_to_slack(
            {"text": "hello"},
            "https://hooks.slack.example/X/Y/Z",
            client=client,
        )
    finally:
        client.close()

    assert captured["method"] == "POST"
    assert captured["url"] == "https://hooks.slack.example/X/Y/Z"
    assert "hello" in captured["body"]


def test_post_to_slack_disables_link_unfurls() -> None:
    import json

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, text="ok")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        post_to_slack(
            {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]},
            "https://hooks.slack.example/X/Y/Z",
            client=client,
        )
    finally:
        client.close()

    assert captured["body"]["unfurl_links"] is False
    assert captured["body"]["unfurl_media"] is False


def test_post_to_slack_does_not_mutate_caller_payload() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    payload: dict[str, Any] = {"text": "hello"}
    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        post_to_slack(payload, "https://hooks.slack.example/X/Y/Z", client=client)
    finally:
        client.close()

    assert "unfurl_links" not in payload
    assert "unfurl_media" not in payload


def test_post_to_slack_raises_on_non_200() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="invalid_payload")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(SlackPostError, match="400"):
            post_to_slack(
                {"blocks": []},
                "https://hooks.slack.example/X/Y/Z",
                client=client,
            )
    finally:
        client.close()


# --- infra alert builder + poster -------------------------------------------


def test_build_infra_alert_basic_shape() -> None:
    payload = build_infra_alert(
        "Haiku cost cap exceeded: 1,200/1,000 tokens today",
        "Classification halted until UTC midnight rollover.",
        severity="fatal",
    )
    assert "blocks" in payload
    assert len(payload["blocks"]) == 1
    block = payload["blocks"][0]
    assert block["type"] == "section"
    text = block["text"]["text"]
    assert "🚨" in text
    assert "FATAL" in text
    assert "Haiku cost cap exceeded" in text
    assert "UTC midnight" in text


def test_build_infra_alert_prefix_prepended_to_subject() -> None:
    payload = build_infra_alert(
        "Thing went wrong",
        "Details here.",
        severity="warn",
        prefix="[INFRA] ",
    )
    text = payload["blocks"][0]["text"]["text"]
    assert "[INFRA] Thing went wrong" in text
    assert "⚠️" in text
    assert "WARN" in text


def test_build_infra_alert_escapes_mrkdwn_metacharacters() -> None:
    payload = build_infra_alert(
        "Cap exceeded: 2 > 1",
        "See <https://dashboard> & verify.",
        severity="fatal",
    )
    text = payload["blocks"][0]["text"]["text"]
    # Escaped to HTML entities per Slack's mrkdwn rules.
    assert "&gt;" in text
    assert "&lt;" in text
    assert "&amp;" in text


def test_post_infra_alert_sends_to_channel_url() -> None:
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = req.content.decode()
        return httpx.Response(200, text="ok")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    channel = InfraAlertChannel(
        webhook_url="https://hooks.slack.example/INFRA",
        source="infra",
        prefix="",
    )
    try:
        post_infra_alert(
            channel,
            subject="cap exceeded",
            body="halted",
            severity="fatal",
            client=client,
        )
    finally:
        client.close()

    assert captured["url"] == "https://hooks.slack.example/INFRA"
    assert "FATAL" in captured["body"]
    assert "cap exceeded" in captured["body"]
