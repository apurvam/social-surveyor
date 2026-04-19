from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest

from social_surveyor.notifier import (
    CATEGORY_COLORS,
    TOP_N_PER_CATEGORY,
    DigestStats,
    NotifierConfig,
    NotifierItem,
    SlackPostError,
    build_digest,
    build_immediate_alert,
    post_to_slack,
)


def _cfg(project: str = "opendata", sv: str = "sv") -> NotifierConfig:
    return NotifierConfig(project=project, sv_command=sv)


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
    alerted_at: datetime | None = None,
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
        alerted_at=alerted_at,
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


def test_immediate_alert_contains_title_urgency_and_correction_commands() -> None:
    payload = build_immediate_alert(_item(), _cfg(project="opendata", sv="sv"))
    text = _all_text(payload["attachments"][0]["blocks"])
    assert "cost_complaint" in text
    assert "urgency 8" in text
    assert "Datadog costs doubled overnight" in text
    # Copy-paste correction lines must include both label and silence.
    assert "sv label --project opendata --item-id hackernews:42" in text
    assert "sv silence --project opendata --item-id hackernews:42" in text
    # And the item id shows in its own context block for easy copying.
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


def test_digest_empty_day_still_sends_a_message() -> None:
    """Zero items in the window must still produce a payload — it's the
    liveness signal that the pipeline is running."""
    payload = build_digest(
        [],
        DigestStats(
            day=date(2026, 4, 19),
            haiku_cost_usd=0.0,
            x_cost_usd=0.0,
            total_labeled=143,
            accuracy_pct=61.5,
        ),
        _cfg(),
    )
    text = _all_text(payload["blocks"])
    assert "Digest for 2026-04-19" in text
    assert "no new items in the last 24h" in text
    # Even on empty days the footer still points at the inspection command.
    assert "sv digest --project opendata --category" in text


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
            x_cost_usd=0.0,
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
            x_cost_usd=0.0,
            total_labeled=0,
        ),
        _cfg(project="opendata", sv="sv"),
    )
    text = _all_text(payload["blocks"])
    # Header line reflects total count + "showing top N".
    assert "active_practitioner" in text
    assert f"{len(items)} items" in text
    assert f"top {TOP_N_PER_CATEGORY} by urgency" in text
    # Top-urgency item present, lowest-urgency item not.
    assert "active item 0" in text  # urgency 10
    assert "active item 7" not in text  # urgency 3, below the top-5 cut
    # Overflow hint names the CLI command.
    assert "3 more active_practitioner items" in text
    assert "sv digest --project opendata --category active_practitioner" in text


def test_digest_no_overflow_hint_when_under_cap() -> None:
    items = [
        _item(item_id=f"hackernews:{i}", category="cost_complaint", urgency=8, title=f"t{i}")
        for i in range(3)
    ]
    payload = build_digest(
        items,
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, x_cost_usd=0, total_labeled=0),
        _cfg(),
    )
    text = _all_text(payload["blocks"])
    assert "more cost_complaint items" not in text
    assert "showing top" not in text


def test_digest_alerted_earlier_section_shown_and_items_not_duplicated() -> None:
    """Alerted items appear only in the alerted-earlier section, not
    also in their category section — deduplicating the user's attention."""
    alerted_at = datetime(2026, 4, 19, 9, 15, tzinfo=UTC)
    alerted = _item(
        item_id="hackernews:100",
        title="URGENT alert item",
        category="cost_complaint",
        urgency=9,
        alerted_at=alerted_at,
    )
    quiet = _item(
        item_id="hackernews:101",
        title="routine cost complaint",
        category="cost_complaint",
        urgency=7,
    )
    payload = build_digest(
        [alerted, quiet],
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, x_cost_usd=0, total_labeled=0),
        _cfg(),
    )
    text = _all_text(payload["blocks"])
    # Alerted-earlier section renders the urgent item with its alert time.
    assert "Alerted earlier today" in text
    assert "URGENT alert item" in text
    assert "alerted at 09:15" in text
    # Alerted item appears exactly once (not duplicated in category section).
    assert text.count("URGENT alert item") == 1
    # Non-alerted item is still in the category section.
    assert "routine cost complaint" in text


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
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, x_cost_usd=0, total_labeled=0),
        _cfg(),
    )
    text = _all_text(payload["blocks"])
    assert "🔕" in text
    assert "noisy tutorial" in text


def test_digest_cost_footer_includes_accuracy_and_cli_pointer() -> None:
    payload = build_digest(
        [_item()],
        DigestStats(
            day=date(2026, 4, 19),
            haiku_cost_usd=0.12,
            x_cost_usd=0.15,
            total_labeled=143,
            accuracy_pct=61.5,
        ),
        _cfg(project="opendata", sv="sv"),
    )
    text = _all_text(payload["blocks"])
    assert "$0.12 Haiku" in text
    assert "$0.15 X" in text
    assert "143 items labeled" in text
    assert "61.5% accuracy" in text
    # Pointer to the category-inspection CLI command.
    assert "sv digest --project opendata --category" in text


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
            x_cost_usd=0.0,
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
    # Correction header in the footer.
    assert any("Correct a classification" in h for h in headers)


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
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, x_cost_usd=0, total_labeled=0),
        _cfg(),
    )
    text = _all_text(payload["blocks"])
    # Plain Slack link mrkdwn — no surrounding bold so the line reads
    # like a news-feed entry rather than a dashboard.
    assert "<https://news.ycombinator.com/item?id=42|Datadog costs doubled>" in text
    # And we're not silently re-adding bold wrappers.
    assert "*<https" not in text


def test_digest_item_without_url_falls_back_to_plain_title() -> None:
    """Items without a URL render as plain text — no stray link syntax
    and no bold wrapper that would lie about clickability."""
    item = _item(url=None, title="No link available")
    payload = build_digest(
        [item],
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, x_cost_usd=0, total_labeled=0),
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
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, x_cost_usd=0, total_labeled=0),
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
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, x_cost_usd=0, total_labeled=0),
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
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, x_cost_usd=0, total_labeled=0),
        _cfg(),
    )
    text = _all_text(payload["blocks"])
    assert "A" * 280 in text
    # And we don't silently extend the cap — 281 still truncates.
    too_long = _item(item_id="x:2", source="x", title="B" * 300, category="neutral_discussion")
    payload2 = build_digest(
        [too_long],
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, x_cost_usd=0, total_labeled=0),
        _cfg(),
    )
    text2 = _all_text(payload2["blocks"])
    assert "B" * 300 not in text2
    assert "…" in text2


def test_digest_items_do_not_include_author() -> None:
    """Author is dropped from per-item lines (low signal; title +
    source do the work)."""
    item = _item(title="some title", author="the-author")
    payload = build_digest(
        [item],
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, x_cost_usd=0, total_labeled=0),
        _cfg(),
    )
    text = _all_text(payload["blocks"])
    assert "the-author" not in text


def test_digest_uses_human_category_labels_when_available() -> None:
    """When category_labels is populated, section headers and the
    alerted-earlier block show the human-friendly label, not the
    snake_case id. The code subtext still uses the id (the CLI accepts
    only ids)."""
    cfg = NotifierConfig(
        project="opendata",
        sv_command="sv",
        category_labels={
            "cost_complaint": "Observability cost complaint",
            "self_host_intent": "Self-host Prometheus intent",
        },
    )
    items = [
        _item(item_id="reddit:1", category="cost_complaint", title="a", urgency=7),
        _item(
            item_id="reddit:2",
            category="cost_complaint",
            title="b",
            urgency=8,
            alerted_at=datetime(2026, 4, 19, 9, 0, tzinfo=UTC),
        ),
    ]
    payload = build_digest(
        items,
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, x_cost_usd=0, total_labeled=0),
        cfg,
    )
    text = _all_text(payload["blocks"])
    # Section header uses the label.
    assert "Observability cost complaint" in text
    # The snake_case id still appears in the footer's "Categories: ..."
    # listing (that's where the list of valid ids lives, since users
    # need to type an id for --category <id>).
    assert "cost_complaint" in text
    # But NOT in per-item subtexts — those only carry --item-id now.
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
            DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, x_cost_usd=0, total_labeled=0),
            _cfg(),
        )["blocks"]
    )
    plural = _all_text(
        build_digest(
            two_items,
            DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, x_cost_usd=0, total_labeled=0),
            _cfg(),
        )["blocks"]
    )
    assert "· 1 item" in single and "· 1 items" not in single
    assert "· 2 items" in plural


def test_digest_correction_footer_lists_all_three_commands() -> None:
    payload = build_digest(
        [_item()],
        DigestStats(day=date(2026, 4, 19), haiku_cost_usd=0, x_cost_usd=0, total_labeled=0),
        _cfg(project="opendata", sv="sv"),
    )
    text = _all_text(payload["blocks"])
    assert "sv label --project opendata" in text
    assert "sv silence --project opendata" in text
    assert "sv ingest --project opendata" in text


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
