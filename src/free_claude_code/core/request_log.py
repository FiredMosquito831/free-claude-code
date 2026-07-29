"""SQLite-backed request log with a non-blocking background writer."""

import contextlib
import json
import queue
import sqlite3
import threading
import time
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
    headers TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts_epoch);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
CREATE INDEX IF NOT EXISTS idx_requests_provider ON requests(provider);
CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(resolved_model);
"""


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
    ttft_ms: float | None = None
    duration_ms: float | None = None
    status: RequestStatus = "success"
    error_kind: str | None = None
    error_message: str | None = None
    headers: dict[str, str] | None = None

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

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------ writes

    def enqueue(self, record: RequestRecord) -> None:
        """Queue one record without blocking the request path."""
        if self._closed.is_set():
            return
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            logger.warning("Request log queue full; dropping record {}", record.id)

    def _writer_loop(self) -> None:
        pending: list[RequestRecord] = []
        stopping = False
        while not stopping:
            try:
                item = self._queue.get(timeout=_WRITER_POLL_SECONDS)
            except queue.Empty:
                item = None
            if item is None:
                if pending:
                    self._flush(pending)
                    pending.clear()
                continue
            if item is _STOP:
                stopping = True
            else:
                pending.append(item)
            if len(pending) >= _WRITER_BATCH_SIZE:
                self._flush(pending)
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
            self._flush(pending)

    def _flush(self, batch: list[RequestRecord]) -> None:
        rows = [self._record_to_row(record) for record in batch]
        try:
            with self._connect() as conn:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO requests (
                        id, ts_epoch, ts_iso, endpoint, protocol, requested_model,
                        provider, resolved_model, stream, input_text, output_text,
                        input_sha256, output_sha256, input_chars, output_chars,
                        reasoning, params, tokens_in, tokens_out, ttft_ms,
                        duration_ms, status, error_kind, error_message, headers
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            record.ttft_ms,
            record.duration_ms,
            record.status,
            record.error_kind,
            cap_text(record.error_message, MAX_ERROR_CHARS),
            json.dumps(record.headers) if record.headers else None,
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
        since: float | None = None,
        until: float | None = None,
        q: str | None = None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if provider:
            clauses.append("provider = ?")
            args.append(provider)
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
            since=since,
            until=until,
            q=q,
        )
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        with self._connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM requests{where}", args
            ).fetchone()[0]
            cursor = conn.execute(
                f"SELECT * FROM requests{where} ORDER BY ts_epoch DESC LIMIT ? OFFSET ?",
                [*args, limit, offset],
            )
            rows = [
                self._row_to_dict(row, body_preview_chars=body_preview_chars)
                for row in cursor.fetchall()
            ]
        return rows, total

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
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
        for key in ("input_text", "output_text"):
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
        for key in ("params", "headers"):
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
        since: float | None = None,
        until: float | None = None,
        q: str | None = None,
    ) -> dict[str, Any]:
        where, args = self._where(
            provider=provider,
            model=model,
            status=status,
            endpoint=endpoint,
            since=since,
            until=until,
            q=q,
        )
        with self._connect() as conn:
            totals = conn.execute(
                f"""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success,
                       SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS error,
                       SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) AS cancelled,
                       COALESCE(SUM(tokens_in), 0) AS tokens_in,
                       COALESCE(SUM(tokens_out), 0) AS tokens_out,
                       AVG(duration_ms) AS avg_duration_ms,
                       AVG(ttft_ms) AS avg_ttft_ms
                FROM requests{where}
                """,
                args,
            ).fetchone()
            durations = [
                row[0]
                for row in conn.execute(
                    f"SELECT duration_ms FROM requests{where}"
                    f"{' AND' if where else ' WHERE'} duration_ms IS NOT NULL"
                    " ORDER BY duration_ms",
                    args,
                ).fetchall()
            ]
            by_provider = self._breakdown(conn, "provider", where, args)
            by_model = self._breakdown(conn, "resolved_model", where, args)
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
            series = self._series(conn, where, args, since=since, until=until)

        total = totals["total"] or 0
        return {
            "window": {"since": since, "until": until},
            "total": total,
            "success": totals["success"] or 0,
            "error": totals["error"] or 0,
            "cancelled": totals["cancelled"] or 0,
            "error_rate": (totals["error"] or 0) / total if total else 0.0,
            "tokens_in": totals["tokens_in"] or 0,
            "tokens_out": totals["tokens_out"] or 0,
            "avg_duration_ms": _rounded(totals["avg_duration_ms"]),
            "p50_duration_ms": _rounded(_percentile(durations, 0.50)),
            "p95_duration_ms": _rounded(_percentile(durations, 0.95)),
            "avg_ttft_ms": _rounded(totals["avg_ttft_ms"]),
            "by_provider": by_provider,
            "by_model": by_model,
            "series": series,
            "top_errors": top_errors,
        }

    @staticmethod
    def _breakdown(
        conn: sqlite3.Connection, column: str, where: str, args: list[Any]
    ) -> list[dict[str, Any]]:
        cursor = conn.execute(
            f"SELECT COALESCE({column}, '(unknown)') AS key, COUNT(*) AS requests,"
            " COALESCE(SUM(tokens_in),0) AS tokens_in,"
            " COALESCE(SUM(tokens_out),0) AS tokens_out,"
            " SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors,"
            " AVG(duration_ms) AS avg_duration_ms"
            f" FROM requests{where} GROUP BY key ORDER BY requests DESC",
            args,
        )
        return [
            {
                "key": row["key"],
                "requests": row["requests"],
                "tokens_in": row["tokens_in"],
                "tokens_out": row["tokens_out"],
                "errors": row["errors"],
                "avg_duration_ms": _rounded(row["avg_duration_ms"]),
            }
            for row in cursor.fetchall()
        ]

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
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "DELETE FROM requests WHERE id IN ("
                    " SELECT id FROM requests ORDER BY ts_epoch DESC"
                    " LIMIT -1 OFFSET ?"
                    ")",
                    (self._max_rows,),
                )
                return cursor.rowcount
        except sqlite3.Error as exc:
            logger.warning("Request log prune failed: {}", exc)
            return 0

    def clear(self) -> int:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM requests")
            return cursor.rowcount


def _percentile(values: list[float], fraction: float) -> float | None:
    """Return a linearly interpolated percentile from an ordered sample."""

    if not values:
        return None
    position = min(len(values) - 1, max(0.0, fraction * (len(values) - 1)))
    lower_index = int(position)
    upper_index = min(len(values) - 1, lower_index + 1)
    weight = position - lower_index
    return values[lower_index] + (values[upper_index] - values[lower_index]) * weight


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
