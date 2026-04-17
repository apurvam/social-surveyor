from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import structlog

from social_surveyor.config import XQuery, XSourceConfig
from social_surveyor.sources.x import XSource
from social_surveyor.storage import Storage

FIXTURE = Path(__file__).parent / "fixtures" / "x_recent_search.json"


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def _handler_returning(payload: dict[str, Any], call_counter: list[int]):
    def handler(request: httpx.Request) -> httpx.Response:
        call_counter[0] += 1
        return httpx.Response(200, json=payload)

    return handler


def test_daily_cap_halts_polling(tmp_path: Path) -> None:
    """When today's usage + max_results > cap, the query is skipped.

    Set daily_read_cap=50 and max_results_per_query=100, pre-seed 0
    usage — the first query's would-fetch (100) already exceeds the
    cap, so we never call the API.
    """
    calls = [0]
    handler = _handler_returning(_fixture(), calls)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = XSourceConfig(
        queries=[
            XQuery(name="q1", query="a"),
            XQuery(name="q2", query="b"),
        ],
        max_results_per_query=100,
        daily_read_cap=50,
    )

    with Storage(tmp_path / "t.db") as db:
        source = XSource(cfg, db, client=client, bearer_token="t")
        with structlog.testing.capture_logs() as cap:
            items = source.fetch()

    assert calls[0] == 0
    assert items == []
    skip_events = [e for e in cap if e.get("event") == "x.daily_cap.skip"]
    assert {e["query_name"] for e in skip_events} == {"q1", "q2"}


def test_daily_cap_permits_one_more_query_then_halts(tmp_path: Path) -> None:
    """Cap at 100; first query returns 50 posts, which puts us at 50.
    Second query's would-fetch (100) + 50 used = 150 > 100, so it's skipped."""
    payload = _fixture()
    payload["meta"]["result_count"] = 50
    calls = [0]
    handler = _handler_returning(payload, calls)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = XSourceConfig(
        queries=[
            XQuery(name="q1", query="a"),
            XQuery(name="q2", query="b"),
        ],
        max_results_per_query=100,
        daily_read_cap=100,
    )
    with Storage(tmp_path / "t.db") as db:
        source = XSource(cfg, db, client=client, bearer_token="t")
        source.fetch()
        start_of_day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        assert db.sum_api_usage("x", start_of_day) == 50

    assert calls[0] == 1  # only q1 hit the API


def test_usage_tracking_survives_across_polls(tmp_path: Path) -> None:
    """Stale usage from prior polls still blocks new queries that'd exceed the cap."""
    calls = [0]
    handler = _handler_returning(_fixture(), calls)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = XSourceConfig(
        queries=[XQuery(name="q", query="q")],
        max_results_per_query=100,
        daily_read_cap=100,
    )
    with Storage(tmp_path / "t.db") as db:
        # Simulate a prior poll today that already used 50 reads.
        db.record_api_usage("x", "q", 50)
        source = XSource(cfg, db, client=client, bearer_token="t")
        source.fetch()

    assert calls[0] == 0  # 50 + 100 > 100


def test_usage_older_than_today_does_not_block(tmp_path: Path) -> None:
    """Yesterday's usage doesn't count against today's cap."""
    calls = [0]
    handler = _handler_returning(_fixture(), calls)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = XSourceConfig(
        queries=[XQuery(name="q", query="q")],
        max_results_per_query=100,
        daily_read_cap=100,
    )
    with Storage(tmp_path / "t.db") as db:
        # Backdate a usage row to two days ago.
        yesterday = datetime.now(UTC) - timedelta(days=2)
        db._conn.execute(
            "INSERT INTO api_usage (source, query_name, items_fetched, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            ("x", "q", 500, yesterday.isoformat()),
        )
        source = XSource(cfg, db, client=client, bearer_token="t")
        source.fetch()

    assert calls[0] == 1


def test_dry_run_state_reports_usage_totals(tmp_path: Path) -> None:
    def forbidden_handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("dry run must not call the API")

    client = httpx.Client(transport=httpx.MockTransport(forbidden_handler))
    cfg = XSourceConfig(
        queries=[XQuery(name="q1", query="a"), XQuery(name="q2", query="b")],
        daily_read_cap=500,
    )
    with Storage(tmp_path / "t.db") as db:
        db.record_api_usage("x", "q1", 40)
        db.record_api_usage("x", "q2", 10)
        db.set_cursor("x", "q1", "12345")
        source = XSource(cfg, db, client=client, bearer_token="t")
        state = source.dry_run_state()

    assert state["used_today"] == 50
    assert state["used_this_month"] == 50
    assert state["daily_read_cap"] == 500
    names = [q["name"] for q in state["queries"]]
    assert names == ["q1", "q2"]
    assert state["queries"][0]["since_id"] == "12345"
    assert state["queries"][1]["since_id"] is None
