from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from social_surveyor.config import (
    CostCapsConfig,
    DigestConfig,
    DigestScheduleConfig,
    ImmediateConfig,
    InfraConfig,
    RoutingConfig,
)
from social_surveyor.cost_caps import (
    HAIKU_CAP_ALERT_KIND,
    HaikuCapCheck,
    check_haiku_cap,
    enforce_haiku_cap,
    resolve_infra_channel,
    today_haiku_tokens,
    today_utc_iso,
)
from social_surveyor.notifier import InfraAlertChannel
from social_surveyor.storage import Storage


def _routing(
    *,
    cap: int = 1000,
    infra_secret: str | None = None,
    immediate_secret: str = "TEST_IMMEDIATE",
) -> RoutingConfig:
    return RoutingConfig(
        version=1,
        immediate=ImmediateConfig(
            threshold_urgency=7,
            alert_worthy_categories=["cost_complaint"],
            webhook_secret=immediate_secret,
        ),
        digest=DigestConfig(
            schedule=DigestScheduleConfig(hour=9, minute=0, timezone="UTC"),
            webhook_secret="TEST_DIGEST",
        ),
        cost_caps=CostCapsConfig(daily_haiku_tokens=cap, daily_x_reads=2000),
        infra=InfraConfig(webhook_secret=infra_secret),
    )


def _seed_usage(db: Storage, input_tokens: int, output_tokens: int) -> None:
    db.record_api_usage(
        source="anthropic",
        query_name="v1",
        items_fetched=1,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


# --- check_haiku_cap --------------------------------------------------------


def test_check_haiku_cap_ok_below_warn_threshold(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        _seed_usage(db, 100, 50)  # 150 / 1000 = 15%
        result = check_haiku_cap(db, cap=1000)
    assert result.state == "ok"
    assert result.today_tokens == 150
    assert result.cap == 1000


def test_check_haiku_cap_warn_at_80_percent(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        _seed_usage(db, 700, 100)  # 800 / 1000 = 80%
        result = check_haiku_cap(db, cap=1000)
    assert result.state == "warn"
    assert result.today_tokens == 800


def test_check_haiku_cap_still_warn_just_under_cap(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        _seed_usage(db, 900, 99)  # 999 / 1000 = 99.9%
        result = check_haiku_cap(db, cap=1000)
    assert result.state == "warn"


def test_check_haiku_cap_halt_at_exactly_cap(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        _seed_usage(db, 900, 100)  # 1000 / 1000 = 100%
        result = check_haiku_cap(db, cap=1000)
    assert result.state == "halt"
    assert result.today_tokens == 1000


def test_check_haiku_cap_halt_above_cap(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        _seed_usage(db, 1200, 100)  # 1300 / 1000 = 130%
        result = check_haiku_cap(db, cap=1000)
    assert result.state == "halt"
    assert result.percent > 100


def test_check_haiku_cap_zero_cap_means_unlimited(tmp_path: Path) -> None:
    """cap=0 sentinels ``unlimited`` — useful in test fixtures."""
    with Storage(tmp_path / "t.db") as db:
        _seed_usage(db, 10_000_000, 1_000_000)
        result = check_haiku_cap(db, cap=0)
    assert result.state == "ok"


def test_today_haiku_tokens_only_counts_today(tmp_path: Path) -> None:
    """Rows from yesterday shouldn't count — the cap resets at UTC midnight."""
    with Storage(tmp_path / "t.db") as db:
        # Today
        _seed_usage(db, 500, 100)
        # Insert a yesterday row directly. record_api_usage stamps now();
        # we bypass it to plant an old row.
        yesterday = datetime.now(UTC) - timedelta(days=1, hours=2)
        db._conn.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO api_usage (source, query_name, items_fetched, fetched_at,
                                   input_tokens, output_tokens)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("anthropic", "v1", 1, yesterday.isoformat(), 10_000, 1_000),
        )
        total = today_haiku_tokens(db)
    assert total == 600  # yesterday's 11k is excluded


def test_today_haiku_tokens_ignores_non_anthropic_sources(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        _seed_usage(db, 100, 50)  # anthropic
        db.record_api_usage(
            source="x",
            query_name="q",
            items_fetched=1,
            input_tokens=None,
            output_tokens=None,
        )
        total = today_haiku_tokens(db)
    assert total == 150


# --- enforce_haiku_cap -------------------------------------------------------


class _StubTransport(httpx.BaseTransport):
    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return httpx.Response(200, text="ok")


def test_enforce_returns_true_below_cap(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        _seed_usage(db, 100, 50)
        assert enforce_haiku_cap(db, _routing(cap=1000)) is True


def test_enforce_warns_but_proceeds_in_warn_band(tmp_path: Path, caplog: Any) -> None:
    with Storage(tmp_path / "t.db") as db:
        _seed_usage(db, 800, 50)  # 850 / 1000 = 85% → warn
        result = enforce_haiku_cap(db, _routing(cap=1000))
    assert result is True
    # No infra_alerts row — warn doesn't post.
    with Storage(tmp_path / "t.db") as db:
        rows = db._conn.execute(  # type: ignore[attr-defined]
            "SELECT COUNT(*) AS c FROM infra_alerts"
        ).fetchone()
        assert rows["c"] == 0


def test_enforce_halts_and_posts_once(tmp_path: Path) -> None:
    """First halt posts to the injected channel; subsequent halts on the
    same UTC day skip the post."""
    transport = _StubTransport()
    client = httpx.Client(transport=transport)
    channel = InfraAlertChannel(
        webhook_url="https://hooks.slack.test/ABC",
        source="infra",
        prefix="",
    )

    with Storage(tmp_path / "t.db") as db:
        _seed_usage(db, 1500, 200)  # 1700 / 1000 → halt

        # First call: halt, post, return False
        r1 = enforce_haiku_cap(db, _routing(cap=1000), infra_channel=channel, http_client=client)
        assert r1 is False
        assert len(transport.calls) == 1

        # Second call (same day): halt again, but no second post
        r2 = enforce_haiku_cap(db, _routing(cap=1000), infra_channel=channel, http_client=client)
        assert r2 is False
        assert len(transport.calls) == 1  # still one

    client.close()


def _seed_usage_at(db: Storage, when: datetime, input_tokens: int, output_tokens: int) -> None:
    """Plant an api_usage row stamped at ``when`` (UTC). ``record_api_usage``
    always stamps now(); this helper bypasses it so tests can simulate
    rows across different days."""
    db._conn.execute(  # type: ignore[attr-defined]
        """
        INSERT INTO api_usage (source, query_name, items_fetched, fetched_at,
                               input_tokens, output_tokens)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "anthropic",
            "v1",
            1,
            when.astimezone(UTC).isoformat(),
            input_tokens,
            output_tokens,
        ),
    )


def test_enforce_posts_each_new_day(tmp_path: Path) -> None:
    """Cap alerts de-dupe on (alert_kind, UTC date). New day → new post."""
    transport = _StubTransport()
    client = httpx.Client(transport=transport)
    channel = InfraAlertChannel(
        webhook_url="https://hooks.slack.test/ABC",
        source="infra",
        prefix="",
    )

    day1 = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    day2 = datetime(2026, 4, 21, 12, 0, tzinfo=UTC)

    with Storage(tmp_path / "t.db") as db:
        # Usage rows for day1 (seen by day1 check) and day2 (seen by day2).
        _seed_usage_at(db, day1, 1500, 200)
        _seed_usage_at(db, day2, 1500, 200)

        enforce_haiku_cap(
            db, _routing(cap=1000), infra_channel=channel, http_client=client, now=day1
        )
        enforce_haiku_cap(
            db, _routing(cap=1000), infra_channel=channel, http_client=client, now=day2
        )

    assert len(transport.calls) == 2
    client.close()


def test_enforce_halts_even_when_no_infra_channel(tmp_path: Path) -> None:
    """Missing webhook shouldn't block the halt — halting is a safety
    behavior independent of whether we can page a human."""
    with Storage(tmp_path / "t.db") as db:
        _seed_usage(db, 1500, 200)
        # infra_channel=None and the resolver returns None because
        # neither env var is set → enforce logs, doesn't post, still halts.
        result = enforce_haiku_cap(db, _routing(cap=1000), infra_channel=None)
    assert result is False


def test_enforce_halts_survives_slack_post_failure(tmp_path: Path) -> None:
    """A 500 from Slack must not block the halt decision."""

    class FailingTransport(httpx.BaseTransport):
        def __init__(self) -> None:
            self.calls = 0

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            self.calls += 1
            return httpx.Response(500, text="slack died")

    transport = FailingTransport()
    client = httpx.Client(transport=transport)
    channel = InfraAlertChannel(
        webhook_url="https://hooks.slack.test/ABC",
        source="infra",
        prefix="",
    )

    with Storage(tmp_path / "t.db") as db:
        _seed_usage(db, 1500, 200)
        result = enforce_haiku_cap(
            db, _routing(cap=1000), infra_channel=channel, http_client=client
        )

    assert result is False
    assert transport.calls == 1
    client.close()


# --- resolve_infra_channel --------------------------------------------------


def test_resolve_prefers_infra_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_INFRA_HOOK", "https://hooks.slack.test/INFRA")
    monkeypatch.setenv("TEST_IMMEDIATE", "https://hooks.slack.test/IMMEDIATE")
    ch = resolve_infra_channel(
        _routing(infra_secret="TEST_INFRA_HOOK", immediate_secret="TEST_IMMEDIATE")
    )
    assert ch is not None
    assert ch.webhook_url == "https://hooks.slack.test/INFRA"
    assert ch.source == "infra"
    assert ch.prefix == ""


def test_resolve_falls_back_to_immediate_with_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Infra secret configured but env var not set → fallback
    monkeypatch.delenv("TEST_INFRA_HOOK", raising=False)
    monkeypatch.setenv("TEST_IMMEDIATE", "https://hooks.slack.test/IMMEDIATE")
    ch = resolve_infra_channel(
        _routing(infra_secret="TEST_INFRA_HOOK", immediate_secret="TEST_IMMEDIATE")
    )
    assert ch is not None
    assert ch.webhook_url == "https://hooks.slack.test/IMMEDIATE"
    assert ch.source == "immediate-fallback"
    assert ch.prefix == "[INFRA] "


def test_resolve_returns_none_when_nothing_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_INFRA_HOOK", raising=False)
    monkeypatch.delenv("TEST_IMMEDIATE", raising=False)
    ch = resolve_infra_channel(
        _routing(infra_secret="TEST_INFRA_HOOK", immediate_secret="TEST_IMMEDIATE")
    )
    assert ch is None


def test_resolve_no_infra_configured_uses_immediate_without_prefix_trial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When routing.infra.webhook_secret is None, the resolver skips
    the infra step and goes straight to the immediate fallback."""
    monkeypatch.setenv("TEST_IMMEDIATE", "https://hooks.slack.test/IMMEDIATE")
    ch = resolve_infra_channel(_routing(infra_secret=None, immediate_secret="TEST_IMMEDIATE"))
    assert ch is not None
    assert ch.source == "immediate-fallback"
    assert ch.prefix == "[INFRA] "


# --- today_utc_iso ----------------------------------------------------------


def test_today_utc_iso_matches_fixed_timestamp() -> None:
    t = datetime(2026, 4, 20, 23, 59, 59, tzinfo=UTC)
    assert today_utc_iso(t) == "2026-04-20"


def test_today_utc_iso_rolls_at_midnight() -> None:
    t = datetime(2026, 4, 21, 0, 0, 1, tzinfo=UTC)
    assert today_utc_iso(t) == "2026-04-21"


# --- HaikuCapCheck percent --------------------------------------------------


def test_percent_formula() -> None:
    c = HaikuCapCheck(state="warn", today_tokens=800, cap=1000)
    assert c.percent == pytest.approx(80.0)


def test_percent_zero_cap_is_zero() -> None:
    c = HaikuCapCheck(state="ok", today_tokens=100, cap=0)
    assert c.percent == 0.0


# --- alert-kind constant self-reference -------------------------------------


def test_alert_kind_constant_is_stable() -> None:
    """Pin the constant — changing it silently would orphan the
    idempotency key and re-page on next deploy."""
    assert HAIKU_CAP_ALERT_KIND == "haiku_cap_exceeded"
