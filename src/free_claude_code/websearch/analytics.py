"""Web search usage analytics: SQLite log store with weekly/monthly rollups.

Recording is non-blocking: :meth:`WebSearchLogStore.record` enqueues onto a
bounded queue drained by a single background writer thread (WAL,
``synchronous=NORMAL``, batched inserts). Reads (``stats``/``requests``) use
short-lived connections so they never block the writer. Retention prunes the
table down to ``max_rows`` newest records every ``prune_every`` inserts.

Import direction: this module may import ``config``/``core`` and the sibling
``registry`` (for the :class:`SearchOutcome` contract); nothing in
``core/websearch`` or ``registry`` imports this module statically — the
registry reaches :func:`record_search` through a dynamic import seam.
"""

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any

from loguru import logger

from free_claude_code.config.paths import FCC_LOGS_DIRNAME, config_dir_path
from free_claude_code.config.settings import Settings

from .registry import SearchOutcome

WEBSEARCH_DB_FILENAME = "websearch.db"
QUERY_LOG_CHARS = 256
ERROR_MESSAGE_LOG_CHARS = 500
STATS_PERIODS: tuple[str, ...] = ("weekly", "monthly")

_DEFAULT_MAX_ROWS = 50000
_QUEUE_CAP = 2048
_BATCH_SIZE = 64
_POLL_SECONDS = 0.2
_PRUNE_EVERY_INSERTS = 100
_MAX_LIMIT = 500
_TOP_ERRORS_LIMIT = 10
_BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_epoch REAL NOT NULL,
    ts_iso TEXT NOT NULL,
    provider TEXT NOT NULL,
    key_index INTEGER NOT NULL,
    key_label TEXT NOT NULL,
    query TEXT NOT NULL,
    results_count INTEGER NOT NULL,
    duration_ms REAL NOT NULL,
    status TEXT NOT NULL,
    error_kind TEXT,
    error_message TEXT,
    cost_usd REAL
);
CREATE INDEX IF NOT EXISTS idx_search_log_ts ON search_log (ts_epoch);
CREATE INDEX IF NOT EXISTS idx_search_log_provider_ts ON search_log (provider, ts_epoch);
"""

_INSERT_SQL = """
INSERT INTO search_log (
    ts_epoch, ts_iso, provider, key_index, key_label, query,
    results_count, duration_ms, status, error_kind, error_message, cost_usd
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_PRUNE_SQL = """
DELETE FROM search_log
WHERE id NOT IN (SELECT id FROM search_log ORDER BY id DESC LIMIT ?)
"""

_REQUEST_COLUMNS = (
    "id, ts_epoch, ts_iso, provider, key_index, key_label, query, results_count,"
    " duration_ms, status, error_kind, error_message, cost_usd"
)


def default_websearch_db_path() -> Path:
    """Default analytics database path: ``~/.fcc/logs/websearch.db``."""

    return config_dir_path() / FCC_LOGS_DIRNAME / WEBSEARCH_DB_FILENAME


class _Control:
    """Writer-thread control message: drain barrier (``clear=False``) or clear."""

    __slots__ = ("clear", "deleted", "done")

    def __init__(self, *, clear: bool) -> None:
        self.clear = clear
        self.done = threading.Event()
        self.deleted = 0


class WebSearchLogStore:
    """Durable per-search usage log with rollup stats.

    ``record`` never blocks the caller (records are dropped with a warning
    when the queue is full). ``close`` drains pending records before the
    writer stops. Instances are safe to share across threads.
    """

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        max_rows: int = _DEFAULT_MAX_ROWS,
        queue_cap: int = _QUEUE_CAP,
        prune_every: int = _PRUNE_EVERY_INSERTS,
    ) -> None:
        self._db_path = db_path if db_path is not None else default_websearch_db_path()
        self._max_rows = max(0, max_rows)
        self._prune_every = max(1, prune_every)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._queue: Queue[SearchOutcome | _Control] = Queue(maxsize=max(1, queue_cap))
        self._state_lock = threading.Lock()
        self._stopping = threading.Event()
        self._closed = False
        self._dropped = 0
        self._inserts_since_prune = 0
        self._writer = threading.Thread(
            target=self._writer_main,
            name="websearch-log-writer",
            daemon=True,
        )
        self._writer.start()

    def __enter__(self) -> WebSearchLogStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def dropped(self) -> int:
        """Records discarded because the queue was full."""

        return self._dropped

    def record(self, outcome: SearchOutcome) -> bool:
        """Enqueue one outcome; False when closed or the queue is full."""

        with self._state_lock:
            if self._closed:
                return False
            try:
                self._queue.put_nowait(outcome)
            except Full:
                self._dropped += 1
                if self._dropped == 1 or self._dropped % 100 == 0:
                    logger.warning(
                        "websearch analytics queue full; dropped {} record(s) total",
                        self._dropped,
                    )
                return False
            return True

    def flush(self, timeout: float = 5.0) -> None:
        """Block until every queued record has been written."""

        with self._state_lock:
            if self._closed:
                return
        self._send_control(_Control(clear=False), timeout)

    def clear(self, timeout: float = 10.0) -> int:
        """Delete every recorded request; returns the number of rows removed."""

        control = _Control(clear=True)
        self._send_control(control, timeout)
        return control.deleted

    def close(self, timeout: float = 5.0) -> bool:
        """Stop the writer after draining queued records; True when drained."""

        with self._state_lock:
            if self._closed:
                return True
            self._closed = True
            self._stopping.set()
        self._writer.join(timeout)
        return not self._writer.is_alive()

    def stats(self, period: str = "weekly") -> dict[str, Any]:
        """Aggregate rollups; ``period`` is ``weekly`` (ISO week) or ``monthly``."""

        if period not in STATS_PERIODS:
            raise ValueError(f"unknown stats period: {period!r}")
        connection = self._connect_reader()
        try:
            totals_row = connection.execute(
                "SELECT COUNT(*) AS requests,"
                " COALESCE(SUM(status = 'error'), 0) AS errors,"
                " AVG(duration_ms) AS avg_duration_ms,"
                " COALESCE(SUM(results_count), 0) AS results,"
                " SUM(cost_usd) AS cost_usd"
                " FROM search_log"
            ).fetchone()
            by_provider = [
                _shaped_aggregate(row)
                for row in connection.execute(
                    "SELECT provider, COUNT(*) AS requests,"
                    " COALESCE(SUM(status = 'error'), 0) AS errors,"
                    " AVG(duration_ms) AS avg_duration_ms,"
                    " COALESCE(SUM(results_count), 0) AS results,"
                    " SUM(cost_usd) AS cost_usd"
                    " FROM search_log GROUP BY provider"
                    " ORDER BY requests DESC, provider ASC"
                ).fetchall()
            ]
            by_key = [
                _shaped_aggregate(row)
                for row in connection.execute(
                    "SELECT provider, key_label, COUNT(*) AS requests,"
                    " COALESCE(SUM(status = 'error'), 0) AS errors,"
                    " AVG(duration_ms) AS avg_duration_ms,"
                    " COALESCE(SUM(results_count), 0) AS results"
                    " FROM search_log GROUP BY provider, key_label"
                    " ORDER BY requests DESC, provider ASC, key_label ASC"
                ).fetchall()
            ]
            top_errors = [
                _row_dict(row)
                for row in connection.execute(
                    "SELECT error_kind, error_message, COUNT(*) AS count"
                    " FROM search_log WHERE status = 'error'"
                    " GROUP BY error_kind, error_message"
                    " ORDER BY count DESC, error_kind ASC, error_message ASC"
                    " LIMIT ?",
                    (_TOP_ERRORS_LIMIT,),
                ).fetchall()
            ]
            series_rows = connection.execute(
                "SELECT ts_epoch, provider, status, results_count FROM search_log"
            ).fetchall()
        finally:
            connection.close()
        totals = _row_dict(totals_row)
        requests_total = int(totals["requests"])
        errors_total = int(totals["errors"])
        return {
            "period": period,
            "totals": {
                "requests": requests_total,
                "successes": requests_total - errors_total,
                "errors": errors_total,
                "avg_duration_ms": _rounded(totals["avg_duration_ms"]),
                "results": int(totals["results"]),
                "cost_usd": totals["cost_usd"],
            },
            "by_provider": by_provider,
            "by_key": by_key,
            "series": _series(series_rows, period),
            "top_errors": top_errors,
        }

    def requests(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        provider: str | None = None,
        status: str | None = None,
        q: str | None = None,
        since_epoch: float | None = None,
        until_epoch: float | None = None,
    ) -> dict[str, Any]:
        """Page recorded requests (newest first) with optional filters."""

        limit = min(max(1, limit), _MAX_LIMIT)
        offset = max(0, offset)
        clauses: list[str] = []
        params: list[Any] = []
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if q:
            clauses.append("instr(query, ?) > 0")
            params.append(q)
        if since_epoch is not None:
            clauses.append("ts_epoch >= ?")
            params.append(since_epoch)
        if until_epoch is not None:
            clauses.append("ts_epoch <= ?")
            params.append(until_epoch)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        connection = self._connect_reader()
        try:
            total = connection.execute(
                f"SELECT COUNT(*) FROM search_log {where}", params
            ).fetchone()[0]
            items = [
                _row_dict(row)
                for row in connection.execute(
                    f"SELECT {_REQUEST_COLUMNS} FROM search_log {where}"
                    " ORDER BY ts_epoch DESC, id DESC LIMIT ? OFFSET ?",
                    (*params, limit, offset),
                ).fetchall()
            ]
        finally:
            connection.close()
        return {"total": int(total), "limit": limit, "offset": offset, "items": items}

    def _send_control(self, control: _Control, timeout: float) -> None:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("websearch log store is closed")
            try:
                self._queue.put(control, timeout=timeout)
            except Full:
                raise TimeoutError("websearch log writer is busy") from None
        if not control.done.wait(timeout):
            raise TimeoutError("websearch log writer did not acknowledge in time")

    def _writer_main(self) -> None:
        connection = sqlite3.connect(self._db_path)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            connection.executescript(_SCHEMA)
            while True:
                batch = self._collect_batch()
                if batch:
                    self._write_batch(connection, batch)
                elif self._stopping.is_set():
                    return
        except Exception:
            logger.exception("websearch analytics writer crashed")
        finally:
            connection.close()

    def _collect_batch(self) -> list[SearchOutcome | _Control]:
        try:
            first = self._queue.get(timeout=_POLL_SECONDS)
        except Empty:
            return []
        items: list[SearchOutcome | _Control] = [first]
        while len(items) < _BATCH_SIZE:
            try:
                items.append(self._queue.get_nowait())
            except Empty:
                break
        return items

    def _write_batch(
        self, connection: sqlite3.Connection, items: list[SearchOutcome | _Control]
    ) -> None:
        rows: list[tuple[object, ...]] = []
        for item in items:
            if isinstance(item, _Control):
                self._insert_rows(connection, rows)
                rows = []
                if item.clear:
                    item.deleted = self._clear_rows(connection)
                item.done.set()
            else:
                rows.append(_row_tuple(item))
        self._insert_rows(connection, rows)

    def _insert_rows(
        self, connection: sqlite3.Connection, rows: list[tuple[object, ...]]
    ) -> None:
        if not rows:
            return
        try:
            connection.executemany(_INSERT_SQL, rows)
            connection.commit()
        except Exception:
            connection.rollback()
            logger.exception(
                "websearch analytics insert failed; dropping {} row(s)", len(rows)
            )
            return
        self._inserts_since_prune += len(rows)
        if self._inserts_since_prune >= self._prune_every:
            self._inserts_since_prune = 0
            self._prune(connection)

    def _clear_rows(self, connection: sqlite3.Connection) -> int:
        cursor = connection.execute("DELETE FROM search_log")
        connection.commit()
        return cursor.rowcount

    def _prune(self, connection: sqlite3.Connection) -> None:
        try:
            connection.execute(_PRUNE_SQL, (self._max_rows,))
            connection.commit()
        except Exception:
            connection.rollback()
            logger.exception("websearch analytics retention prune failed")

    def _connect_reader(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        connection.executescript(_SCHEMA)
        return connection


_shared_lock = threading.Lock()
_shared_store: WebSearchLogStore | None = None
_log_enabled_cache: bool | None = None


def get_shared_store() -> WebSearchLogStore:
    """Lazily created process-wide store (``max_rows`` from settings)."""

    global _shared_store
    with _shared_lock:
        store = _shared_store
        if store is None or store.closed:
            store = WebSearchLogStore(max_rows=_settings_max_rows())
            _shared_store = store
        return store


def record_search(outcome: SearchOutcome) -> None:
    """Registry recorder seam: persist one outcome unless logging is disabled.

    Never raises into the search path; failures are logged and dropped.
    """

    try:
        if not _log_enabled():
            return
        get_shared_store().record(outcome)
    except Exception:
        logger.exception("failed to record web search usage")


def reset_analytics_state() -> None:
    """Close the shared store and drop cached flags (tests/settings reloads)."""

    global _log_enabled_cache, _shared_store
    with _shared_lock:
        store = _shared_store
        _shared_store = None
        _log_enabled_cache = None
    if store is not None:
        store.close()


def _log_enabled() -> bool:
    """``WEBSEARCH_LOG_ENABLED`` (default True), cached at first use."""

    global _log_enabled_cache
    with _shared_lock:
        if _log_enabled_cache is None:
            _log_enabled_cache = Settings().websearch_log_enabled
        return _log_enabled_cache


def _settings_max_rows() -> int:
    try:
        return Settings().websearch_log_max_rows
    except Exception:
        logger.exception("failed to read WEBSEARCH_LOG_MAX_ROWS; using default")
        return _DEFAULT_MAX_ROWS


def _row_tuple(outcome: SearchOutcome) -> tuple[object, ...]:
    return (
        outcome.ts_epoch,
        outcome.ts_iso,
        outcome.provider,
        outcome.key_index,
        outcome.key_label,
        outcome.query[:QUERY_LOG_CHARS],
        outcome.results_count,
        outcome.duration_ms,
        outcome.status,
        outcome.error_kind,
        (
            outcome.error_message[:ERROR_MESSAGE_LOG_CHARS]
            if outcome.error_message
            else None
        ),
        outcome.cost_usd,
    )


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(zip(row.keys(), row, strict=True))


def _shaped_aggregate(row: sqlite3.Row) -> dict[str, Any]:
    shaped = _row_dict(row)
    shaped["requests"] = int(shaped["requests"])
    shaped["errors"] = int(shaped["errors"])
    shaped["avg_duration_ms"] = _rounded(shaped["avg_duration_ms"])
    shaped["results"] = int(shaped["results"])
    return shaped


def _series(rows: list[sqlite3.Row], period: str) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        moment = datetime.fromtimestamp(row["ts_epoch"], tz=UTC)
        if period == "weekly":
            iso = moment.isocalendar()
            bucket = f"{iso.year}-W{iso.week:02d}"
        else:
            bucket = f"{moment.year}-{moment.month:02d}"
        key = (bucket, row["provider"])
        entry = buckets.setdefault(
            key,
            {
                "bucket": bucket,
                "provider": row["provider"],
                "requests": 0,
                "errors": 0,
                "results": 0,
            },
        )
        entry["requests"] += 1
        entry["errors"] += 1 if row["status"] == "error" else 0
        entry["results"] += row["results_count"]
    return sorted(
        buckets.values(), key=lambda entry: (entry["bucket"], entry["provider"])
    )


def _rounded(value: Any) -> float | None:
    return round(float(value), 3) if value is not None else None
