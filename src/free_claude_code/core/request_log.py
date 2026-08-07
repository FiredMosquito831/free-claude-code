"""SQLite-backed request log with a non-blocking background writer."""

import contextlib
import json
import queue
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from loguru import logger

# ``core`` must not import ``config`` (import-boundary contract), so the
# ``~/.fcc`` dirname convention from ``config.paths`` is mirrored here.
_FCC_CONFIG_DIRNAME = ".fcc"

RequestStatus = Literal["success", "error", "cancelled"]

MAX_TEXT_CHARS = 50_000
MAX_ERROR_CHARS = 2_000
LIST_BODY_PREVIEW_CHARS = 4_096
_PRUNE_EVERY_INSERTS = 100
_WRITER_BATCH_SIZE = 50
_WRITER_POLL_SECONDS = 0.25
_QUEUE_MAX_SIZE = 10_000
_STOP = object()
_STATS_CACHE_TTL_SECONDS = 5.0
# Bounds the stats cache to the most recently used filter combinations. Without
# this, every distinct filter tuple a user tries leaks an entry holding a full
# stats payload for the lifetime of the process.
_STATS_CACHE_MAX_ENTRIES = 64
# Caps each breakdown (by provider/model/key) so a gateway with hundreds of
# distinct models does not return hundreds of rows on every poll.
_BREAKDOWN_LIMIT = 50

# Columns read for list views. Body columns are deliberately excluded and
# replaced by SQL-side ``substr`` previews so list queries never load full
# request/response bodies into memory just to truncate them in Python.
_LIST_METADATA_COLUMNS = (
    "id",
    "ts_epoch",
    "ts_iso",
    "endpoint",
    "protocol",
    "requested_model",
    "provider",
    "resolved_model",
    "stream",
    "input_sha256",
    "output_sha256",
    "input_chars",
    "output_chars",
    "reasoning",
    "params",
    "tokens_in",
    "tokens_out",
    "cache_read_tokens",
    "cache_write_tokens",
    "ttft_ms",
    "duration_ms",
    "status",
    "error_kind",
    "error_message",
    "headers",
    "route_attempt",
    "route_primary_model",
    "key_index",
    "key_label",
    # Shape of the assistant turn. These are counts, not bodies, so list views
    # can show what a turn contained without loading the transcript.
    "thinking_chars",
    "tool_call_count",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    ts_epoch REAL NOT NULL,
    ts_iso TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    protocol TEXT NOT NULL,
    requested_model TEXT,
    provider TEXT,
    resolved_model TEXT,
    route_attempt INTEGER,
    route_primary_model TEXT,
    stream INTEGER NOT NULL DEFAULT 0,
    input_text TEXT,
    output_text TEXT,
    input_sha256 TEXT,
    output_sha256 TEXT,
    input_chars INTEGER,
    output_chars INTEGER,
    reasoning TEXT,
    params TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    ttft_ms REAL,
    duration_ms REAL,
    status TEXT NOT NULL,
    error_kind TEXT,
    error_message TEXT,
    headers TEXT,
    key_index INTEGER,
    key_label TEXT,
    thinking_text TEXT,
    thinking_chars INTEGER,
    tool_calls TEXT,
    tool_call_count INTEGER
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts_epoch);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
CREATE INDEX IF NOT EXISTS idx_requests_provider ON requests(provider);
CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(resolved_model);
"""

# Columns added after the initial release. ``CREATE TABLE IF NOT EXISTS`` is a
# no-op on an existing database, so each one needs an explicit ALTER TABLE.
_ADDED_COLUMNS = (
    ("key_index", "ALTER TABLE requests ADD COLUMN key_index INTEGER"),
    ("key_label", "ALTER TABLE requests ADD COLUMN key_label TEXT"),
    ("cache_read_tokens", "ALTER TABLE requests ADD COLUMN cache_read_tokens INTEGER"),
    (
        "cache_write_tokens",
        "ALTER TABLE requests ADD COLUMN cache_write_tokens INTEGER",
    ),
    ("thinking_text", "ALTER TABLE requests ADD COLUMN thinking_text TEXT"),
    ("thinking_chars", "ALTER TABLE requests ADD COLUMN thinking_chars INTEGER"),
    ("tool_calls", "ALTER TABLE requests ADD COLUMN tool_calls TEXT"),
    ("tool_call_count", "ALTER TABLE requests ADD COLUMN tool_call_count INTEGER"),
    ("route_attempt", "ALTER TABLE requests ADD COLUMN route_attempt INTEGER"),
    (
        "route_primary_model",
        "ALTER TABLE requests ADD COLUMN route_primary_model TEXT",
    ),
)

# Indexes over post-release columns, created only once those columns exist.
# Keeping them out of ``_SCHEMA`` matters: that script runs before the ALTER
# TABLE migration, so indexing ``key_label`` there would fail outright on a
# database created by an earlier version.
_ADDED_INDEXES = ("CREATE INDEX IF NOT EXISTS idx_requests_key ON requests(key_label)",)


def default_request_log_path() -> Path:
    """Return the canonical request log database path."""

    return Path.home() / _FCC_CONFIG_DIRNAME / "logs" / "requests.db"


def cap_text(text: str | None, limit: int = MAX_TEXT_CHARS) -> str | None:
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[:limit]


@dataclass(slots=True)
class RequestRecord:
    """One completed request, queued for the background writer."""

    id: str
    endpoint: str
    protocol: str
    ts_epoch: float = field(default_factory=time.time)
    requested_model: str | None = None
    provider: str | None = None
    resolved_model: str | None = None
    # 0 when the route's own model answered, 1+ when a fallback did. ``None``
    # on rows written before fallback chains existed, which is distinct from
    # 0 and must stay that way: an old row cannot claim it used its primary.
    route_attempt: int | None = None
    # The model the route resolved to first, recorded only when a later
    # attempt answered -- otherwise it just repeats ``resolved_model``.
    route_primary_model: str | None = None
    stream: bool = False
    input_text: str | None = None
    output_text: str | None = None
    input_sha256: str | None = None
    output_sha256: str | None = None
    input_chars: int | None = None
    output_chars: int | None = None
    reasoning: str | None = None
    params: dict[str, Any] | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    # Anthropic reports these beside input_tokens; tokens_in is the
    # *uncached* portion, so total input is the sum of all three.
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    ttft_ms: float | None = None
    duration_ms: float | None = None
    status: RequestStatus = "success"
    error_kind: str | None = None
    error_message: str | None = None
    headers: dict[str, str] | None = None
    # Which credential served this request: pool index plus a masked
    # ``first4…last4`` label. The raw key is never stored.
    key_index: int | None = None
    key_label: str | None = None
    # An assistant turn streams three kinds of block. ``output_text`` holds only
    # the model's prose; reasoning and tool calls are kept apart so the detail
    # view can show each for what it is, and so a tool-only turn (the common
    # case under Claude Code) still records what the model actually did.
    thinking_text: str | None = None
    thinking_chars: int | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_count: int | None = None

    @property
    def ts_iso(self) -> str:
        return datetime.fromtimestamp(self.ts_epoch, tz=UTC).isoformat()


class RequestLogStore:
    """Durable per-request log drained by a single background writer thread."""

    def __init__(self, db_path: Path | str, *, max_rows: int = 50_000) -> None:
        self._db_path = Path(db_path)
        self._max_rows = max(0, max_rows)
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=_QUEUE_MAX_SIZE)
        self._inserts_since_prune = 0
        self._closed = threading.Event()
        self._stats_lock = threading.Lock()
        # OrderedDict as an LRU: ``move_to_end`` on every hit/insert keeps the
        # least recently used filter combination at the front for eviction.
        self._stats_cache: OrderedDict[
            tuple[Any, ...], tuple[float, dict[str, Any]]
        ] = OrderedDict()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._writer = threading.Thread(
            target=self._writer_loop,
            name="fcc-request-log-writer",
            daemon=True,
        )
        self._writer.start()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextlib.contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection that is always closed.

        ``sqlite3.Connection.__exit__`` only commits or rolls back; it never
        closes. Connections are garbage-collected rather than reference-counted,
        so relying on scope exit leaks file descriptors until the next GC pass.
        """
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            # A fresh database can adopt incremental auto-vacuum for free, but
            # only if the pragma is applied outside a transaction and before the
            # first table exists. Converting a populated database needs a full
            # VACUUM, which the writer thread performs in the background
            # instead (see ``_writer_loop``).
            previous = conn.isolation_level
            conn.isolation_level = None
            try:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            finally:
                conn.isolation_level = previous
            with conn:
                conn.executescript(_SCHEMA)
                self._ensure_added_columns(conn)
        finally:
            conn.close()

    @staticmethod
    def _ensure_added_columns(conn: sqlite3.Connection) -> None:
        """Add post-release columns to a database created by an older version."""
        existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(requests)")}
        for column, alter_sql in _ADDED_COLUMNS:
            if column in existing:
                continue
            try:
                conn.execute(alter_sql)
            except sqlite3.OperationalError:
                # Another process may have won the migration race; only a
                # genuinely missing column is an error.
                columns = {
                    str(row[1]) for row in conn.execute("PRAGMA table_info(requests)")
                }
                if column not in columns:
                    raise
        for index_sql in _ADDED_INDEXES:
            conn.execute(index_sql)

    @staticmethod
    def _ensure_stats_index(conn: sqlite3.Connection) -> None:
        """Add a covering index for the aggregate queries.

        SQLite stores every column of a row together, so a scan over the
        numeric columns ``stats`` needs still walks the overflow pages holding
        up to 100k characters of request/response text per row. An index that
        carries those columns lets the aggregates run index-only and skip the
        bodies entirely.
        """
        with contextlib.suppress(sqlite3.Error):
            # Versioned name: ``CREATE INDEX IF NOT EXISTS`` would silently keep
            # an older index built before ``key_label`` joined the column list,
            # leaving the per-key aggregate without index-only coverage.
            conn.execute("DROP INDEX IF EXISTS idx_requests_stats")
            conn.execute("DROP INDEX IF EXISTS idx_requests_stats_v2")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_requests_stats_v3 ON requests("
                " ts_epoch, status, provider, resolved_model, endpoint,"
                " requested_model, key_label, duration_ms, ttft_ms,"
                " tokens_in, tokens_out, cache_read_tokens, cache_write_tokens)"
            )

    @staticmethod
    def _ensure_auto_vacuum(conn: sqlite3.Connection) -> None:
        """Enable incremental auto-vacuum so pruned pages can be reclaimed.

        Without this the database file only ever grows: ``prune`` frees pages
        onto the internal freelist but never returns them to the filesystem.

        Converting an existing database requires a full VACUUM, which on a
        multi-hundred-megabyte file takes many seconds. This must therefore run
        on the writer thread, never on a request path.
        """
        try:
            mode = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
            if int(mode) == 2:
                return
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            previous = conn.isolation_level
            conn.isolation_level = None
            try:
                started = time.monotonic()
                conn.execute("VACUUM")
                logger.info(
                    "Request log converted to incremental auto-vacuum in {:.1f}s",
                    time.monotonic() - started,
                )
            finally:
                conn.isolation_level = previous
        except sqlite3.Error as exc:
            logger.warning("Request log auto_vacuum setup failed: {}", exc)

    # ------------------------------------------------------------------ writes

    def enqueue(self, record: RequestRecord) -> None:
        """Queue one record without blocking the request path."""
        if self._closed.is_set():
            return
        # Cap before queueing, not at flush time: an uncapped record sits in the
        # queue holding its full body, so a backlog could retain far more than
        # the persisted per-row limit.
        record.input_text = cap_text(record.input_text)
        record.output_text = cap_text(record.output_text)
        record.error_message = cap_text(record.error_message, MAX_ERROR_CHARS)
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            logger.warning("Request log queue full; dropping record {}", record.id)

    def _writer_loop(self) -> None:
        pending: list[RequestRecord] = []
        stopping = False
        # One connection for the writer thread's lifetime; reconnecting per
        # batch re-runs the WAL/synchronous pragmas on every flush.
        conn = self._connect()
        try:
            # Both of these are one-time migrations that can take seconds on a
            # large existing database, so they belong here and never on a
            # request path.
            self._ensure_stats_index(conn)
            self._ensure_auto_vacuum(conn)
            while not stopping:
                try:
                    item = self._queue.get(timeout=_WRITER_POLL_SECONDS)
                except queue.Empty:
                    item = None
                if item is None:
                    if pending:
                        self._flush(pending, conn)
                        pending.clear()
                    continue
                if item is _STOP:
                    stopping = True
                else:
                    pending.append(item)
                if len(pending) >= _WRITER_BATCH_SIZE:
                    self._flush(pending, conn)
                    pending.clear()
            # Drain anything enqueued behind the stop sentinel, then exit.
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is not None and item is not _STOP:
                    pending.append(item)
            if pending:
                self._flush(pending, conn)
        finally:
            conn.close()

    def _flush(self, batch: list[RequestRecord], conn: sqlite3.Connection) -> None:
        rows = [self._record_to_row(record) for record in batch]
        try:
            with conn:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO requests (
                        id, ts_epoch, ts_iso, endpoint, protocol, requested_model,
                        provider, resolved_model, stream, input_text, output_text,
                        input_sha256, output_sha256, input_chars, output_chars,
                        reasoning, params, tokens_in, tokens_out,
                        cache_read_tokens, cache_write_tokens, ttft_ms,
                        duration_ms, status, error_kind, error_message, headers,
                        key_index, key_label, thinking_text, thinking_chars,
                        tool_calls, tool_call_count, route_attempt,
                        route_primary_model
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    rows,
                )
        except sqlite3.Error as exc:
            logger.warning("Request log write failed: {}", exc)
            return
        self._inserts_since_prune += len(batch)
        if self._inserts_since_prune >= _PRUNE_EVERY_INSERTS:
            self._inserts_since_prune = 0
            self.prune()

    @staticmethod
    def _record_to_row(record: RequestRecord) -> tuple[Any, ...]:
        return (
            record.id,
            record.ts_epoch,
            record.ts_iso,
            record.endpoint,
            record.protocol,
            record.requested_model,
            record.provider,
            record.resolved_model,
            int(record.stream),
            cap_text(record.input_text),
            cap_text(record.output_text),
            record.input_sha256,
            record.output_sha256,
            record.input_chars,
            record.output_chars,
            record.reasoning,
            json.dumps(record.params) if record.params is not None else None,
            record.tokens_in,
            record.tokens_out,
            record.cache_read_tokens,
            record.cache_write_tokens,
            record.ttft_ms,
            record.duration_ms,
            record.status,
            record.error_kind,
            cap_text(record.error_message, MAX_ERROR_CHARS),
            json.dumps(record.headers) if record.headers else None,
            record.key_index,
            record.key_label,
            cap_text(record.thinking_text),
            record.thinking_chars,
            json.dumps(record.tool_calls) if record.tool_calls else None,
            record.tool_call_count,
            record.route_attempt,
            record.route_primary_model,
        )

    def close(self, *, timeout: float = 5.0) -> None:
        """Stop the writer thread after flushing queued records."""
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            with contextlib.suppress(queue.Full):
                self._queue.put(_STOP, timeout=timeout)
        self._writer.join(timeout=timeout)

    # ------------------------------------------------------------------ reads

    def _where(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        status: str | None = None,
        endpoint: str | None = None,
        key: str | None = None,
        since: float | None = None,
        until: float | None = None,
        q: str | None = None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if provider:
            clauses.append("provider = ?")
            args.append(provider)
        if key:
            clauses.append("key_label = ?")
            args.append(key)
        if model:
            clauses.append("(resolved_model = ? OR requested_model = ?)")
            args.extend([model, model])
        if status:
            clauses.append("status = ?")
            args.append(status)
        if endpoint:
            clauses.append("endpoint = ?")
            args.append(endpoint)
        if since is not None:
            clauses.append("ts_epoch >= ?")
            args.append(since)
        if until is not None:
            clauses.append("ts_epoch <= ?")
            args.append(until)
        if q:
            clauses.append("(input_text LIKE ? OR output_text LIKE ?)")
            pattern = f"%{q}%"
            args.extend([pattern, pattern])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, args

    def list_requests(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        provider: str | None = None,
        model: str | None = None,
        status: str | None = None,
        endpoint: str | None = None,
        key: str | None = None,
        since: float | None = None,
        until: float | None = None,
        q: str | None = None,
        body_preview_chars: int | None = LIST_BODY_PREVIEW_CHARS,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return (rows, total) newest-first, with bodies truncated for list views."""
        where, args = self._where(
            provider=provider,
            model=model,
            status=status,
            endpoint=endpoint,
            key=key,
            since=since,
            until=until,
            q=q,
        )
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        if body_preview_chars is None:
            body_select = "input_text, output_text"
            body_args: list[Any] = []
        else:
            preview = max(0, body_preview_chars)
            body_select = (
                "substr(input_text, 1, ?) AS input_text,"
                " length(input_text) AS input_text_length,"
                " substr(output_text, 1, ?) AS output_text,"
                " length(output_text) AS output_text_length"
            )
            body_args = [preview, preview]
        columns = ", ".join(_LIST_METADATA_COLUMNS)
        with self._connection() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM requests{where}", args
            ).fetchone()[0]
            cursor = conn.execute(
                f"SELECT {columns}, {body_select} FROM requests{where}"
                " ORDER BY ts_epoch DESC LIMIT ? OFFSET ?",
                [*body_args, *args, limit, offset],
            )
            rows = [
                self._row_to_dict(row, body_preview_chars=body_preview_chars)
                for row in cursor.fetchall()
            ]
        return rows, total

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            cursor = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,))
            row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row, body_preview_chars=None)

    @staticmethod
    def _row_to_dict(
        row: sqlite3.Row, *, body_preview_chars: int | None
    ) -> dict[str, Any]:
        data = dict(row)
        data["stream"] = bool(data["stream"])
        # ``thinking_text`` is only projected by the detail query; list views
        # carry ``thinking_chars`` instead, so skip whatever is absent.
        body_keys = [
            key
            for key in ("input_text", "output_text", "thinking_text")
            if key in data or f"{key}_length" in data
        ]
        for key in body_keys:
            # List queries project a SQL-side preview plus the untruncated
            # length, so the full body never reaches Python.
            length = data.pop(f"{key}_length", None)
            if length is not None:
                data[f"{key}_truncated"] = (
                    body_preview_chars is not None and int(length) > body_preview_chars
                )
                continue
            text = data.get(key)
            if (
                body_preview_chars is not None
                and isinstance(text, str)
                and len(text) > body_preview_chars
            ):
                data[key] = text[:body_preview_chars]
                data[f"{key}_truncated"] = True
            else:
                data[f"{key}_truncated"] = False
        for key in ("params", "headers", "tool_calls"):
            raw = data.get(key)
            if isinstance(raw, str):
                try:
                    data[key] = json.loads(raw)
                except json.JSONDecodeError:
                    data[key] = None
        return data

    # ------------------------------------------------------------------ stats

    def stats(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        status: str | None = None,
        endpoint: str | None = None,
        key: str | None = None,
        since: float | None = None,
        until: float | None = None,
        q: str | None = None,
    ) -> dict[str, Any]:
        cache_key = (provider, model, status, endpoint, key, since, until, q)
        now = time.monotonic()
        with self._stats_lock:
            cached = self._stats_cache.get(cache_key)
            if cached is not None:
                if now - cached[0] < _STATS_CACHE_TTL_SECONDS:
                    self._stats_cache.move_to_end(cache_key)
                    return dict(cached[1])
                # Expired: drop it now rather than waiting for LRU eviction to
                # get around to it.
                del self._stats_cache[cache_key]
        where, args = self._where(
            provider=provider,
            model=model,
            status=status,
            endpoint=endpoint,
            key=key,
            since=since,
            until=until,
            q=q,
        )
        with self._connection() as conn:
            totals = conn.execute(
                f"""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success,
                       SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS error,
                       SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) AS cancelled,
                       COALESCE(SUM(tokens_in), 0) AS tokens_in,
                       COALESCE(SUM(tokens_out), 0) AS tokens_out,
                       COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                       COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                       SUM(CASE WHEN cache_read_tokens IS NOT NULL THEN 1 ELSE 0 END)
                           AS cache_reported,
                       COALESCE(SUM(tool_call_count), 0) AS tool_calls,
                       SUM(CASE WHEN tool_call_count > 0 THEN 1 ELSE 0 END)
                           AS turns_with_tools,
                       SUM(CASE WHEN thinking_chars > 0 THEN 1 ELSE 0 END)
                           AS turns_with_reasoning,
                       SUM(CASE WHEN route_attempt > 0 THEN 1 ELSE 0 END)
                           AS served_by_fallback,
                       SUM(CASE WHEN route_attempt IS NOT NULL THEN 1 ELSE 0 END)
                           AS route_reported,
                       AVG(duration_ms) AS avg_duration_ms,
                       AVG(ttft_ms) AS avg_ttft_ms
                FROM requests{where}
                """,
                args,
            ).fetchone()
            percentiles = self._percentiles(conn, where, args, (0.50, 0.95))
            by_provider, by_provider_truncated = self._breakdown(
                conn, "provider", where, args
            )
            by_model, by_model_truncated = self._breakdown(
                conn, "resolved_model", where, args
            )
            by_key, by_key_truncated = self._breakdown(conn, "key_label", where, args)
            top_errors = [
                {"message": row[0], "count": row[1]}
                for row in conn.execute(
                    f"SELECT error_message, COUNT(*) FROM requests{where}"
                    f"{' AND' if where else ' WHERE'} status='error'"
                    " AND error_message IS NOT NULL"
                    " GROUP BY error_message ORDER BY COUNT(*) DESC LIMIT 10",
                    args,
                ).fetchall()
            ]
            fallback_routes = [
                {
                    "primary": row[0],
                    "served_by": row[1],
                    "count": row[2],
                }
                for row in conn.execute(
                    f"SELECT route_primary_model,"
                    " COALESCE(provider, '(unknown)') || '/' ||"
                    " COALESCE(resolved_model, '(unknown)'), COUNT(*)"
                    f" FROM requests{where}"
                    f"{' AND' if where else ' WHERE'} route_attempt > 0"
                    " AND route_primary_model IS NOT NULL"
                    " GROUP BY 1, 2 ORDER BY COUNT(*) DESC LIMIT 10",
                    args,
                ).fetchall()
            ]
            series = self._series(conn, where, args, since=since, until=until)

        total = totals["total"] or 0
        payload = {
            "window": {"since": since, "until": until},
            "total": total,
            "success": totals["success"] or 0,
            "error": totals["error"] or 0,
            "cancelled": totals["cancelled"] or 0,
            "error_rate": (totals["error"] or 0) / total if total else 0.0,
            "tokens_in": totals["tokens_in"] or 0,
            "tokens_out": totals["tokens_out"] or 0,
            "cache_read_tokens": totals["cache_read_tokens"] or 0,
            "cache_write_tokens": totals["cache_write_tokens"] or 0,
            "cache_reported": totals["cache_reported"] or 0,
            "tool_calls": totals["tool_calls"] or 0,
            "turns_with_tools": totals["turns_with_tools"] or 0,
            "turns_with_reasoning": totals["turns_with_reasoning"] or 0,
            # ``route_reported`` separates "no fallback was used" from "these
            # rows predate fallback chains", so the UI can show a dash rather
            # than a reassuring 0% for traffic it knows nothing about.
            "served_by_fallback": totals["served_by_fallback"] or 0,
            "route_reported": totals["route_reported"] or 0,
            "fallback_routes": fallback_routes,
            "avg_duration_ms": _rounded(totals["avg_duration_ms"]),
            "p50_duration_ms": _rounded(percentiles[0.50]),
            "p95_duration_ms": _rounded(percentiles[0.95]),
            "avg_ttft_ms": _rounded(totals["avg_ttft_ms"]),
            "by_provider": by_provider,
            "by_provider_truncated": by_provider_truncated,
            "by_model": by_model,
            "by_model_truncated": by_model_truncated,
            "by_key": by_key,
            "by_key_truncated": by_key_truncated,
            "series": series,
            "top_errors": top_errors,
        }
        with self._stats_lock:
            self._stats_cache[cache_key] = (now, payload)
            self._stats_cache.move_to_end(cache_key)
            while len(self._stats_cache) > _STATS_CACHE_MAX_ENTRIES:
                self._stats_cache.popitem(last=False)
        return dict(payload)

    def pulse(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        status: str | None = None,
        endpoint: str | None = None,
        key: str | None = None,
        since: float | None = None,
        until: float | None = None,
        q: str | None = None,
    ) -> dict[str, Any]:
        """Return a cheap heartbeat: row count and latest timestamp for these filters.

        Auto-refresh polls this instead of ``stats()``: a single COUNT/MAX query
        lets the caller detect "nothing changed" without paying for percentiles,
        breakdowns, or series buckets on every tick.
        """
        where, args = self._where(
            provider=provider,
            model=model,
            status=status,
            endpoint=endpoint,
            key=key,
            since=since,
            until=until,
            q=q,
        )
        with self._connection() as conn:
            total, last_ts = conn.execute(
                f"SELECT COUNT(*), MAX(ts_epoch) FROM requests{where}", args
            ).fetchone()
        return {"total": total or 0, "last_ts": last_ts}

    @staticmethod
    def _breakdown(
        conn: sqlite3.Connection, column: str, where: str, args: list[Any]
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return (rows, truncated) for a GROUP BY breakdown, capped at ``_BREAKDOWN_LIMIT``.

        Fetches one row past the cap to detect truncation without a second
        COUNT(DISTINCT ...) query, then trims it back off before returning.
        """
        cursor = conn.execute(
            f"SELECT COALESCE({column}, '(unknown)') AS key, COUNT(*) AS requests,"
            " COALESCE(SUM(tokens_in),0) AS tokens_in,"
            " COALESCE(SUM(tokens_out),0) AS tokens_out,"
            " COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens,"
            " COALESCE(SUM(cache_write_tokens),0) AS cache_write_tokens,"
            " SUM(CASE WHEN cache_read_tokens IS NOT NULL THEN 1 ELSE 0 END)"
            " AS cache_reported,"
            " SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors,"
            " AVG(duration_ms) AS avg_duration_ms"
            f" FROM requests{where} GROUP BY key ORDER BY requests DESC LIMIT ?",
            [*args, _BREAKDOWN_LIMIT + 1],
        )
        rows = cursor.fetchall()
        truncated = len(rows) > _BREAKDOWN_LIMIT
        rows = rows[:_BREAKDOWN_LIMIT]
        return [
            {
                "key": row["key"],
                "requests": row["requests"],
                "tokens_in": row["tokens_in"],
                "tokens_out": row["tokens_out"],
                "cache_read_tokens": row["cache_read_tokens"],
                "cache_write_tokens": row["cache_write_tokens"],
                "cache_reported": row["cache_reported"],
                "errors": row["errors"],
                "avg_duration_ms": _rounded(row["avg_duration_ms"]),
            }
            for row in rows
        ], truncated

    @staticmethod
    def _percentiles(
        conn: sqlite3.Connection,
        where: str,
        args: list[Any],
        fractions: tuple[float, ...],
    ) -> dict[float, float | None]:
        """Compute percentiles from one ordered pass over ``duration_ms``.

        Two cleverer mechanisms were tried and measured, and both lost to this:

        - An index leading on ``duration_ms`` makes an isolated rank lookup 68x
          faster, and made the whole of ``stats()`` 2.2x slower (1525 ms against
          701 ms). With no ``ANALYZE`` statistics SQLite starts preferring it for
          the totals and breakdown aggregates it does not cover.
        - Streaming the sorted cursor and stopping once the highest needed rank
          has gone past measured 1.5x (unfiltered) to 1.7x (provider-filtered)
          the cost of a plain ``fetchall()``. ``p95`` needs a rank near the end
          of the row count whatever the filter, so there is almost nothing to
          stop early from, while ``fetchall()`` is one bulk C-level fetch
          against a Python-level ``__next__`` per row.

        So this is the same one query and one fetch the removed ``_percentile``
        helper used, with p50 and p95 sharing a single sorted list instead of
        two separate module-level calls. It is not faster than what it replaces;
        the wins in this area are the bounded stats cache, the capped
        breakdowns, and ``pulse()``.

        Interpolation matches the removed helper's formula exactly.
        """
        connector = " AND" if where else " WHERE"
        values = [
            row[0]
            for row in conn.execute(
                f"SELECT duration_ms FROM requests{where}{connector}"
                " duration_ms IS NOT NULL ORDER BY duration_ms",
                args,
            ).fetchall()
        ]
        if not values:
            return dict.fromkeys(fractions)

        count = len(values)
        results: dict[float, float | None] = {}
        for fraction in fractions:
            position = min(count - 1, max(0.0, fraction * (count - 1)))
            lower_index = int(position)
            upper_index = min(count - 1, lower_index + 1)
            weight = position - lower_index
            lower_val = values[lower_index]
            upper_val = values[upper_index]
            results[fraction] = lower_val + (upper_val - lower_val) * weight
        return results

    @staticmethod
    def _series(
        conn: sqlite3.Connection,
        where: str,
        args: list[Any],
        *,
        since: float | None,
        until: float | None,
    ) -> list[dict[str, Any]]:
        bounds = conn.execute(
            f"SELECT MIN(ts_epoch), MAX(ts_epoch) FROM requests{where}", args
        ).fetchone()
        low = since if since is not None else bounds[0]
        high = until if until is not None else bounds[1]
        hourly = low is not None and high is not None and (high - low) < 48 * 3600
        fmt = "%Y-%m-%dT%H:00" if hourly else "%Y-%m-%d"
        cursor = conn.execute(
            "SELECT strftime(?, ts_epoch, 'unixepoch') AS bucket, COUNT(*) AS requests,"
            " COALESCE(SUM(tokens_in),0) + COALESCE(SUM(tokens_out),0) AS tokens,"
            " SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors"
            f" FROM requests{where} GROUP BY bucket ORDER BY bucket",
            [fmt, *args],
        )
        return [
            {
                "bucket": row["bucket"],
                "requests": row["requests"],
                "tokens": row["tokens"],
                "errors": row["errors"],
            }
            for row in cursor.fetchall()
            if row["bucket"] is not None
        ]

    # -------------------------------------------------------------- maintenance

    def prune(self) -> int:
        """Delete oldest rows beyond the configured retention cap."""
        if self._max_rows <= 0:
            return 0
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    "DELETE FROM requests WHERE id IN ("
                    " SELECT id FROM requests ORDER BY ts_epoch DESC"
                    " LIMIT -1 OFFSET ?"
                    ")",
                    (self._max_rows,),
                )
                removed = cursor.rowcount
            if removed:
                # Return the freed pages to the filesystem instead of leaving
                # them on the freelist, where they would grow the file forever.
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("PRAGMA incremental_vacuum")
            return removed
        except sqlite3.Error as exc:
            logger.warning("Request log prune failed: {}", exc)
            return 0
        finally:
            conn.close()

    def clear(self) -> int:
        with self._stats_lock:
            self._stats_cache.clear()
        with self._connection() as conn:
            cursor = conn.execute("DELETE FROM requests")
            return cursor.rowcount


def _rounded(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


# --------------------------------------------------------------------- registry

_store_lock = threading.Lock()
_stores: dict[Path, RequestLogStore] = {}


def get_request_log_store(
    db_path: Path | str | None = None,
    *,
    max_rows: int = 50_000,
    enabled: bool = True,
) -> RequestLogStore | None:
    """Return the shared store for a database path, creating it on first use."""
    if not enabled:
        return None
    path = Path(db_path) if db_path is not None else default_request_log_path()
    with _store_lock:
        store = _stores.get(path)
        if store is None or store._closed.is_set():
            store = RequestLogStore(path, max_rows=max_rows)
            _stores[path] = store
        return store


def reset_request_log_stores() -> None:
    """Close and forget all shared stores (test isolation / shutdown)."""
    with _store_lock:
        stores = list(_stores.values())
        _stores.clear()
    for store in stores:
        store.close()


def store_from_settings(settings: Any) -> RequestLogStore | None:
    """Resolve the shared store for the active settings, if logging is enabled."""
    if not getattr(settings, "request_log_enabled", True):
        return None
    return get_request_log_store(
        max_rows=int(getattr(settings, "request_log_max_rows", 50_000) or 50_000),
    )
