from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .types import RawItem

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS items (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        source       TEXT    NOT NULL,
        platform_id  TEXT    NOT NULL,
        url          TEXT    NOT NULL,
        title        TEXT    NOT NULL,
        body         TEXT,
        author       TEXT,
        created_at   TEXT    NOT NULL,
        fetched_at   TEXT    NOT NULL,
        raw_json     TEXT    NOT NULL,
        UNIQUE(source, platform_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_items_source_created ON items(source, created_at DESC)",
    # Per-(source, cursor_key) incremental cursor. HN tracks the highest
    # created_at_i per query; X tracks the highest tweet id per query_name.
    # cursor_value is TEXT to accommodate both numeric timestamps and
    # opaque platform tokens.
    """
    CREATE TABLE IF NOT EXISTS source_cursors (
        source       TEXT NOT NULL,
        cursor_key   TEXT NOT NULL,
        cursor_value TEXT NOT NULL,
        updated_at   TEXT NOT NULL,
        PRIMARY KEY (source, cursor_key)
    )
    """,
    # Per-call API usage log for cost tracking. X is the only paid
    # source today; Reddit/HN/GitHub don't insert here. One row per poll
    # call; sum by day or month for cost reporting.
    """
    CREATE TABLE IF NOT EXISTS api_usage (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        source         TEXT    NOT NULL,
        query_name     TEXT    NOT NULL,
        items_fetched  INTEGER NOT NULL,
        fetched_at     TEXT    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_api_usage_source_fetched ON api_usage(source, fetched_at)",
)


def _to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _from_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


class Storage:
    """Thin SQLite wrapper.

    One ``.db`` file per project. The file is created on first connection;
    :data:`SCHEMA_STATEMENTS` is idempotent so multiple processes can
    initialize concurrently without racing.

    No migration framework in the MVP — sessions that add tables append
    to :data:`SCHEMA_STATEMENTS`. Re-structuring existing columns will
    need a migration story before it happens.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.db_path,
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,  # autocommit; we manage transactions explicitly
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._initialize()

    def _initialize(self) -> None:
        with self._conn:
            for stmt in SCHEMA_STATEMENTS:
                self._conn.execute(stmt)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def upsert_item(self, item: RawItem) -> bool:
        """Insert ``item`` if new; no-op if ``(source, platform_id)`` already exists.

        Returns True when a new row was inserted, False otherwise.
        """
        fetched_at = _to_iso(datetime.now(UTC))
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO items (
                    source, platform_id, url, title, body, author,
                    created_at, fetched_at, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, platform_id) DO NOTHING
                """,
                (
                    item.source,
                    item.platform_id,
                    item.url,
                    item.title,
                    item.body,
                    item.author,
                    _to_iso(item.created_at),
                    fetched_at,
                    json.dumps(item.raw_json, default=str),
                ),
            )
            return cur.rowcount == 1

    def get_items(
        self,
        limit: int = 100,
        offset: int = 0,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        if source is None:
            rows = self._conn.execute(
                "SELECT * FROM items ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM items WHERE source = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (source, limit, offset),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def count_items(self, source: str | None = None) -> int:
        if source is None:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM items WHERE source = ?", (source,)
            ).fetchone()
        return int(row["c"])

    # --- cursors ---------------------------------------------------------

    def get_cursor(self, source: str, cursor_key: str) -> str | None:
        row = self._conn.execute(
            "SELECT cursor_value FROM source_cursors WHERE source = ? AND cursor_key = ?",
            (source, cursor_key),
        ).fetchone()
        return None if row is None else str(row["cursor_value"])

    def set_cursor(self, source: str, cursor_key: str, cursor_value: str) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO source_cursors (source, cursor_key, cursor_value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source, cursor_key) DO UPDATE SET
                    cursor_value = excluded.cursor_value,
                    updated_at = excluded.updated_at
                """,
                (source, cursor_key, cursor_value, _to_iso(datetime.now(UTC))),
            )

    def get_cursors(self, source: str) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT cursor_key, cursor_value FROM source_cursors WHERE source = ?",
            (source,),
        ).fetchall()
        return {r["cursor_key"]: r["cursor_value"] for r in rows}

    # --- api usage -------------------------------------------------------

    def record_api_usage(self, source: str, query_name: str, items_fetched: int) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO api_usage (source, query_name, items_fetched, fetched_at)
                VALUES (?, ?, ?, ?)
                """,
                (source, query_name, items_fetched, _to_iso(datetime.now(UTC))),
            )

    def sum_api_usage(self, source: str, since: datetime) -> int:
        row = self._conn.execute(
            """
            SELECT COALESCE(SUM(items_fetched), 0) AS total
            FROM api_usage
            WHERE source = ? AND fetched_at >= ?
            """,
            (source, _to_iso(since)),
        ).fetchone()
        return int(row["total"])

    def api_usage_by_query(self, source: str, since: datetime) -> dict[str, int]:
        rows = self._conn.execute(
            """
            SELECT query_name, COALESCE(SUM(items_fetched), 0) AS total
            FROM api_usage
            WHERE source = ? AND fetched_at >= ?
            GROUP BY query_name
            ORDER BY total DESC
            """,
            (source, _to_iso(since)),
        ).fetchall()
        return {r["query_name"]: int(r["total"]) for r in rows}

    # --- helpers ---------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        if d.get("raw_json") is not None:
            d["raw_json"] = json.loads(d["raw_json"])
        if d.get("created_at") is not None:
            d["created_at"] = _from_iso(d["created_at"])
        if d.get("fetched_at") is not None:
            d["fetched_at"] = _from_iso(d["fetched_at"])
        return d
