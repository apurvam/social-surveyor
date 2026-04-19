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
    # Session 3: Haiku classifications. Multiple rows per item are
    # allowed (different prompt versions, or re-classifications under
    # the same version). The authoritative row for a
    # (item_id, prompt_version) pair is the one with the latest
    # classified_at.
    """
    CREATE TABLE IF NOT EXISTS classifications (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id         TEXT    NOT NULL,
        category        TEXT    NOT NULL,
        urgency         INTEGER NOT NULL,
        reasoning       TEXT,
        prompt_version  TEXT    NOT NULL,
        model           TEXT    NOT NULL,
        input_tokens    INTEGER,
        output_tokens   INTEGER,
        classified_at   TEXT    NOT NULL,
        raw_response    TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_classifications_item_id ON classifications(item_id)",
    "CREATE INDEX IF NOT EXISTS idx_classifications_prompt_version "
    "ON classifications(prompt_version)",
    # Session 4: user-marked "stop alerting me about this item." Distinct
    # from label corrections — silencing does NOT teach the classifier.
    # Keyed on item_id because the silence persists across prompt-version
    # re-classifications; a new classification under v4 for an item the
    # user silenced under v3 is still silenced.
    """
    CREATE TABLE IF NOT EXISTS silenced_items (
        item_id     TEXT PRIMARY KEY,
        silenced_at TEXT NOT NULL
    )
    """,
    # Session 4: routing decisions. One row per (classification, channel).
    # Created when the router decides where a classification goes;
    # sent_at is set when the Slack post succeeds. Querying unrouted
    # classifications = classifications with no alerts row. Querying
    # pending immediate alerts = channel='immediate' AND sent_at IS NULL.
    """
    CREATE TABLE IF NOT EXISTS alerts (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id           TEXT    NOT NULL,
        classification_id INTEGER NOT NULL,
        channel           TEXT    NOT NULL,
        queued_at         TEXT    NOT NULL,
        sent_at           TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_alerts_channel_sent ON alerts(channel, sent_at)",
    "CREATE INDEX IF NOT EXISTS idx_alerts_classification ON alerts(classification_id)",
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
            self._apply_migrations()

    def _apply_migrations(self) -> None:
        """Additive schema changes needed for databases created by older sessions.

        Session 3 added ``input_tokens`` / ``output_tokens`` to
        ``api_usage`` for Anthropic cost tracking. Fresh DBs get these
        columns from the CREATE above by virtue of a later ADD; existing
        opendata.db instances get them via the ALTER below. Other
        sources leave these columns NULL.

        Not a migration framework — PLAN.md explicitly defers that. This
        is the narrow "add a nullable column, don't touch anything
        else" pattern called out as safe.
        """
        self._maybe_add_column("api_usage", "input_tokens", "INTEGER")
        self._maybe_add_column("api_usage", "output_tokens", "INTEGER")

    def _maybe_add_column(self, table: str, column: str, ddl: str) -> None:
        existing = {
            row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in existing:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

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

    def record_api_usage(
        self,
        source: str,
        query_name: str,
        items_fetched: int,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        """Log one API call. Token columns stay NULL for non-LLM sources."""
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO api_usage (
                    source, query_name, items_fetched, fetched_at,
                    input_tokens, output_tokens
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    query_name,
                    items_fetched,
                    _to_iso(datetime.now(UTC)),
                    input_tokens,
                    output_tokens,
                ),
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

    # --- stats helpers ---------------------------------------------------

    _UNKNOWN_GROUP = "(unknown query)"

    def count_items_by_window(self, since: datetime | None = None) -> dict[str, int]:
        """Count items per ``source`` since ``since`` (None = all time)."""
        if since is None:
            rows = self._conn.execute(
                "SELECT source, COUNT(*) AS c FROM items GROUP BY source"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT source, COUNT(*) AS c FROM items WHERE created_at >= ? GROUP BY source",
                (_to_iso(since),),
            ).fetchall()
        return {r["source"]: int(r["c"]) for r in rows}

    def count_items_by_group(self, since: datetime | None = None) -> list[tuple[str, int]]:
        """Items per ``raw_json.group_key``, newest-first by count.

        Pre-``group_key`` items are surfaced under the literal
        ``(unknown query)`` bucket rather than silently dropped —
        see Session 2.5's decision to avoid inference-based backfill.
        """
        where = ""
        params: tuple[object, ...] = ()
        if since is not None:
            where = "WHERE created_at >= ?"
            params = (_to_iso(since),)
        rows = self._conn.execute(
            f"""
            SELECT
                COALESCE(json_extract(raw_json, '$.group_key'), ?) AS group_key,
                COUNT(*) AS c
            FROM items
            {where}
            GROUP BY group_key
            ORDER BY c DESC
            """,
            (self._UNKNOWN_GROUP, *params),
        ).fetchall()
        return [(str(r["group_key"]), int(r["c"])) for r in rows]

    def list_item_ids(self, source: str | None = None) -> list[str]:
        """Return canonical ``{source}:{platform_id}`` ids for every item.

        Used by the labeler to build the "already-labeled" exclusion set
        and the unlabeled queue.
        """
        if source is None:
            rows = self._conn.execute(
                "SELECT source, platform_id FROM items ORDER BY created_at DESC"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT source, platform_id FROM items WHERE source = ? ORDER BY created_at DESC",
                (source,),
            ).fetchall()
        return [f"{r['source']}:{r['platform_id']}" for r in rows]

    def get_item_by_id(self, source: str, platform_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM items WHERE source = ? AND platform_id = ?",
            (source, platform_id),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def list_items_in_group(
        self,
        group_key: str,
        *,
        limit: int = 8,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Items in a configured-query bucket, newest-first.

        ``group_key`` of :attr:`_UNKNOWN_GROUP` returns items whose
        ``raw_json.group_key`` is absent.
        """
        if group_key == self._UNKNOWN_GROUP:
            rows = self._conn.execute(
                """
                SELECT * FROM items
                WHERE json_extract(raw_json, '$.group_key') IS NULL
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT * FROM items
                WHERE json_extract(raw_json, '$.group_key') = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (group_key, limit, offset),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # --- api usage (continued) -------------------------------------------

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

    def sum_api_tokens(
        self,
        source: str,
        since: datetime,
    ) -> tuple[int, int]:
        """Return (input_tokens, output_tokens) totals since ``since``.

        Rows with NULL token columns (non-LLM calls, or pre-Session-3
        rows) contribute 0. Used by the ``usage --source anthropic`` CLI
        to report classification spend.
        """
        row = self._conn.execute(
            """
            SELECT
                COALESCE(SUM(input_tokens), 0)  AS in_total,
                COALESCE(SUM(output_tokens), 0) AS out_total
            FROM api_usage
            WHERE source = ? AND fetched_at >= ?
            """,
            (source, _to_iso(since)),
        ).fetchone()
        return int(row["in_total"]), int(row["out_total"])

    # --- classifications -------------------------------------------------

    def save_classification(
        self,
        *,
        item_id: str,
        category: str,
        urgency: int,
        reasoning: str,
        prompt_version: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        classified_at: datetime,
        raw_response: dict[str, Any],
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO classifications (
                    item_id, category, urgency, reasoning,
                    prompt_version, model,
                    input_tokens, output_tokens,
                    classified_at, raw_response
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    category,
                    urgency,
                    reasoning,
                    prompt_version,
                    model,
                    input_tokens,
                    output_tokens,
                    _to_iso(classified_at),
                    json.dumps(raw_response, default=str),
                ),
            )

    def get_classification(
        self,
        item_id: str,
        prompt_version: str,
    ) -> dict[str, Any] | None:
        """Latest classification for ``(item_id, prompt_version)``.

        Multiple rows are allowed per pair (re-classifications); this
        returns the most recent by ``classified_at``.
        """
        row = self._conn.execute(
            """
            SELECT * FROM classifications
            WHERE item_id = ? AND prompt_version = ?
            ORDER BY classified_at DESC, id DESC
            LIMIT 1
            """,
            (item_id, prompt_version),
        ).fetchone()
        if row is None:
            return None
        return self._classification_row_to_dict(row)

    def list_classifications(self, item_id: str) -> list[dict[str, Any]]:
        """Every classification for ``item_id``, newest first.

        Used by the ``explain`` command to show prompt-version history.
        """
        rows = self._conn.execute(
            """
            SELECT * FROM classifications
            WHERE item_id = ?
            ORDER BY classified_at DESC, id DESC
            """,
            (item_id,),
        ).fetchall()
        return [self._classification_row_to_dict(r) for r in rows]

    def count_classifications(
        self,
        *,
        prompt_version: str | None = None,
        category: str | None = None,
    ) -> int:
        where: list[str] = []
        params: list[Any] = []
        if prompt_version is not None:
            where.append("prompt_version = ?")
            params.append(prompt_version)
        if category is not None:
            where.append("category = ?")
            params.append(category)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        row = self._conn.execute(
            f"SELECT COUNT(*) AS c FROM classifications {clause}",
            tuple(params),
        ).fetchone()
        return int(row["c"])

    def get_unclassified_items(
        self,
        prompt_version: str,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Items that have no classification under ``prompt_version``.

        Ordered oldest-first on purpose: in backfill/classify mode we
        want deterministic progress from the tail, not a re-shuffle
        every time new items arrive.
        """
        sql = """
            SELECT i.* FROM items i
            WHERE (i.source || ':' || i.platform_id) NOT IN (
                SELECT item_id FROM classifications WHERE prompt_version = ?
            )
            ORDER BY i.created_at ASC, i.id ASC
        """
        params: tuple[Any, ...] = (prompt_version,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (prompt_version, limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # --- routing / alerts ------------------------------------------------

    def list_unrouted_classifications(
        self,
        *,
        prompt_version: str | None = None,
    ) -> list[dict[str, Any]]:
        """Classifications with no alerts row (under the given version).

        Returned rows include the classification fields plus ``item_id``
        so routers don't need a second query. Ordered oldest-first so
        retries and multi-run sessions make deterministic progress.
        """
        where = ["NOT EXISTS (SELECT 1 FROM alerts a WHERE a.classification_id = c.id)"]
        params: list[Any] = []
        if prompt_version is not None:
            where.append("c.prompt_version = ?")
            params.append(prompt_version)
        clause = " AND ".join(where)
        rows = self._conn.execute(
            f"""
            SELECT c.*
            FROM classifications c
            WHERE {clause}
            ORDER BY c.classified_at ASC, c.id ASC
            """,
            tuple(params),
        ).fetchall()
        return [self._classification_row_to_dict(r) for r in rows]

    def record_alert(
        self,
        *,
        item_id: str,
        classification_id: int,
        channel: str,
        sent_at: datetime | None = None,
    ) -> int:
        """Create an alerts row. Returns the new row id.

        ``sent_at`` is usually None at insert time (the router decides,
        the sender marks sent). For a one-shot post (e.g. a digest that
        built and sent atomically) callers pass ``sent_at=now``.
        """
        queued_at = _to_iso(datetime.now(UTC))
        sent_iso = _to_iso(sent_at) if sent_at is not None else None
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO alerts (item_id, classification_id, channel, queued_at, sent_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (item_id, classification_id, channel, queued_at, sent_iso),
            )
        return int(cur.lastrowid or 0)

    def mark_alert_sent(self, alert_id: int, sent_at: datetime) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE alerts SET sent_at = ? WHERE id = ?",
                (_to_iso(sent_at), alert_id),
            )

    def list_pending_alerts(self, channel: str) -> list[dict[str, Any]]:
        """Alerts in ``channel`` that haven't been posted yet.

        Joins in the item + classification so the caller can build
        Slack payloads without additional queries.
        """
        rows = self._conn.execute(
            """
            SELECT
                a.id AS alert_id,
                a.item_id AS item_id,
                a.queued_at AS queued_at,
                c.category AS category,
                c.urgency AS urgency,
                c.reasoning AS reasoning,
                c.classified_at AS classified_at,
                i.source AS source,
                i.title AS title,
                i.body AS body,
                i.author AS author,
                i.url AS url,
                i.created_at AS created_at
            FROM alerts a
            JOIN classifications c ON a.classification_id = c.id
            JOIN items i ON i.source = substr(a.item_id, 1, instr(a.item_id, ':') - 1)
                        AND i.platform_id = substr(a.item_id, instr(a.item_id, ':') + 1)
            WHERE a.channel = ? AND a.sent_at IS NULL
            ORDER BY c.classified_at ASC
            """,
            (channel,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            if d.get("created_at"):
                d["created_at"] = _from_iso(d["created_at"])
            if d.get("classified_at"):
                d["classified_at"] = _from_iso(d["classified_at"])
            if d.get("queued_at"):
                d["queued_at"] = _from_iso(d["queued_at"])
            out.append(d)
        return out

    def list_alerts_in_window(
        self,
        *,
        channel: str,
        since: datetime,
        include_unsent: bool = False,
    ) -> list[dict[str, Any]]:
        """Alerts on ``channel`` whose sent_at (or queued_at if unsent)
        falls in ``[since, now]``.

        Used by the digest builder to assemble:
          - ``channel='immediate'``, ``include_unsent=False``: already-
            alerted items for the "alerted earlier" section.
          - ``channel='digest'``, ``include_unsent=True``: items queued
            for this digest cycle.
        """
        since_iso = _to_iso(since)
        if include_unsent:
            # Unsent alerts are only relevant if queued in-window; otherwise
            # a --since <future-date> filter would still return every
            # unsent alert regardless of when it was queued.
            where = "a.channel = ? AND ((a.sent_at IS NULL AND a.queued_at >= ?) OR a.sent_at >= ?)"
            params: tuple[Any, ...] = (channel, since_iso, since_iso)
        else:
            where = "a.channel = ? AND a.sent_at IS NOT NULL AND a.sent_at >= ?"
            params = (channel, since_iso)
        rows = self._conn.execute(
            f"""
            SELECT
                a.id AS alert_id,
                a.item_id AS item_id,
                a.queued_at AS queued_at,
                a.sent_at AS sent_at,
                c.category AS category,
                c.urgency AS urgency,
                c.reasoning AS reasoning,
                c.classified_at AS classified_at,
                i.source AS source,
                i.title AS title,
                i.body AS body,
                i.author AS author,
                i.url AS url,
                i.created_at AS created_at
            FROM alerts a
            JOIN classifications c ON a.classification_id = c.id
            JOIN items i ON i.source = substr(a.item_id, 1, instr(a.item_id, ':') - 1)
                        AND i.platform_id = substr(a.item_id, instr(a.item_id, ':') + 1)
            WHERE {where}
            ORDER BY c.urgency DESC, c.classified_at DESC
            """,
            params,
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            for k in ("created_at", "classified_at", "queued_at", "sent_at"):
                if d.get(k):
                    d[k] = _from_iso(d[k])
            out.append(d)
        return out

    # --- silenced items --------------------------------------------------

    def silence_item(self, item_id: str) -> bool:
        """Silence ``item_id``. Idempotent.

        Returns True when the row is newly inserted, False when the item
        was already silenced. Callers use the return for user messaging
        ("silenced" vs "was already silenced") — the underlying DB
        state is the same either way.
        """
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO silenced_items (item_id, silenced_at)
                VALUES (?, ?)
                ON CONFLICT(item_id) DO NOTHING
                """,
                (item_id, _to_iso(datetime.now(UTC))),
            )
            return cur.rowcount == 1

    def is_silenced(self, item_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM silenced_items WHERE item_id = ? LIMIT 1",
            (item_id,),
        ).fetchone()
        return row is not None

    def silenced_since(self, since: datetime) -> set[str]:
        """Item IDs silenced on or after ``since``.

        Used by the digest to render the 🔕 marker against items
        silenced within the digest window — older silences are hidden
        entirely (see Session 4 design notes).
        """
        rows = self._conn.execute(
            "SELECT item_id FROM silenced_items WHERE silenced_at >= ?",
            (_to_iso(since),),
        ).fetchall()
        return {r["item_id"] for r in rows}

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

    @staticmethod
    def _classification_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        if d.get("raw_response"):
            d["raw_response"] = json.loads(d["raw_response"])
        if d.get("classified_at") is not None:
            d["classified_at"] = _from_iso(d["classified_at"])
        return d
