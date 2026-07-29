"""Web search usage analytics: SQLite log store with configurable rollups.

Recording is non-blocking: :meth:`WebSearchLogStore.record` enqueues onto a
bounded queue drained by a single background writer thread (WAL,
``synchronous=NORMAL``, batched inserts). Reads (``stats``/``requests``) use
short-lived connections so they never block the writer. Retention prunes both
attempt and logical-route tables to ``max_rows`` newest records.

Import direction: this module may import ``config``/``core`` and the sibling
``registry`` (for the outcome contracts); nothing in
``core/websearch`` or ``registry`` imports this module statically — the
registry reaches the recorders through dynamic import seams.
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

from .registry import SearchOutcome, SearchRouteOutcome

WEBSEARCH_DB_FILENAME = "websearch.db"
QUERY_LOG_CHARS = 256
ERROR_MESSAGE_LOG_CHARS = 500
STATS_PERIODS: tuple[str, ...] = ("hourly", "daily", "weekly", "monthly")

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
    cost_usd REAL,
    route_id TEXT,
    attempt_number INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS search_route_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL UNIQUE,
    ts_epoch REAL NOT NULL,
    ts_iso TEXT NOT NULL,
    query TEXT NOT NULL,
    primary_provider TEXT NOT NULL,
    terminal_provider TEXT NOT NULL,
    provider_path TEXT NOT NULL,
    attempt_count INTEGER NOT NULL,
    fallback_used INTEGER NOT NULL,
    duration_ms REAL NOT NULL,
    status TEXT NOT NULL,
    results_count INTEGER NOT NULL,
    cost_usd REAL,
    error_kind TEXT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_search_log_ts ON search_log (ts_epoch);
CREATE INDEX IF NOT EXISTS idx_search_log_provider_ts ON search_log (provider, ts_epoch);
CREATE INDEX IF NOT EXISTS idx_search_route_log_ts
    ON search_route_log (ts_epoch);
CREATE INDEX IF NOT EXISTS idx_search_route_log_terminal_ts
    ON search_route_log (terminal_provider, ts_epoch);
"""

_POST_MIGRATION_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_search_log_route_id ON search_log (route_id);
"""

_INSERT_SQL = """
INSERT INTO search_log (
    ts_epoch, ts_iso, provider, key_index, key_label, query,
    results_count, duration_ms, status, error_kind, error_message, cost_usd,
    route_id, attempt_number
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_ROUTE_SQL = """
INSERT INTO search_route_log (
    route_id, ts_epoch, ts_iso, query, primary_provider, terminal_provider,
    provider_path, attempt_count, fallback_used, duration_ms, status,
    results_count, cost_usd, error_kind, error_message
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_PRUNE_SQL = """
DELETE FROM search_log
WHERE id NOT IN (SELECT id FROM search_log ORDER BY id DESC LIMIT ?)
"""

_PRUNE_ROUTES_SQL = """
DELETE FROM search_route_log
WHERE id NOT IN (SELECT id FROM search_route_log ORDER BY id DESC LIMIT ?)
"""

_REQUEST_COLUMNS = (
    "id, ts_epoch, ts_iso, provider, key_index, key_label, query, results_count,"
    " duration_ms, status, error_kind, error_message, cost_usd, route_id,"
    " attempt_number"
)

_ROUTE_COLUMNS = (
    "id, route_id, ts_epoch, ts_iso, query, primary_provider, terminal_provider,"
    " provider_path, attempt_count, fallback_used, duration_ms, status,"
    " results_count, cost_usd, error_kind, error_message"
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
        self._queue: Queue[SearchOutcome | SearchRouteOutcome | _Control] = Queue(
            maxsize=max(1, queue_cap)
        )
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
        """Enqueue one provider-attempt outcome."""

        return self._enqueue(outcome)

    def record_route(self, outcome: SearchRouteOutcome) -> bool:
        """Enqueue one logical route outcome."""

        return self._enqueue(outcome)

    def _enqueue(self, outcome: SearchOutcome | SearchRouteOutcome) -> bool:
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

    def stats(
        self,
        period: str = "weekly",
        *,
        provider: str | None = None,
        status: str | None = None,
        q: str | None = None,
        since_epoch: float | None = None,
        until_epoch: float | None = None,
    ) -> dict[str, Any]:
        """Aggregate a consistently filtered row set using ``period`` buckets."""

        if period not in STATS_PERIODS:
            raise ValueError(f"unknown stats period: {period!r}")
        attempt_where, attempt_params = _attempt_filter_where(
            provider=provider,
            status=status,
            q=q,
            since_epoch=since_epoch,
            until_epoch=until_epoch,
        )
        route_where, route_params = _route_filter_where(
            provider=provider,
            status=status,
            q=q,
            since_epoch=since_epoch,
            until_epoch=until_epoch,
        )
        connection = self._connect_reader()
        try:
            attempts = _attempt_stats(connection, attempt_where, attempt_params, period)
            routes = _route_stats(connection, route_where, route_params, period)
        finally:
            connection.close()
        route_window = routes["window"]
        attempt_window = attempts["window"]
        return {
            "period": period,
            "filters": {
                "provider": provider,
                "status": status,
                "q": q,
                "since_epoch": since_epoch,
                "until_epoch": until_epoch,
            },
            "window": {
                "since_epoch": (
                    since_epoch
                    if since_epoch is not None
                    else (
                        route_window["since_epoch"]
                        if route_window["since_epoch"] is not None
                        else attempt_window["since_epoch"]
                    )
                ),
                "until_epoch": (
                    until_epoch
                    if until_epoch is not None
                    else (
                        route_window["until_epoch"]
                        if route_window["until_epoch"] is not None
                        else attempt_window["until_epoch"]
                    )
                ),
            },
            "dropped_records": self.dropped,
            # Compatibility aliases: these remain provider-attempt metrics.
            "totals": attempts["totals"],
            "by_provider": attempts["by_provider"],
            "by_key": attempts["by_key"],
            "series": attempts["series"],
            "top_errors": attempts["top_errors"],
            # Explicit layers for new clients.
            "attempts": attempts,
            "routes": routes,
            "last_route": routes["last_route"],
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
        where, params = _attempt_filter_where(
            provider=provider,
            status=status,
            q=q,
            since_epoch=since_epoch,
            until_epoch=until_epoch,
        )
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
            _initialize_schema(connection)
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

    def _collect_batch(
        self,
    ) -> list[SearchOutcome | SearchRouteOutcome | _Control]:
        try:
            first = self._queue.get(timeout=_POLL_SECONDS)
        except Empty:
            return []
        items: list[SearchOutcome | SearchRouteOutcome | _Control] = [first]
        while len(items) < _BATCH_SIZE:
            try:
                items.append(self._queue.get_nowait())
            except Empty:
                break
        return items

    def _write_batch(
        self,
        connection: sqlite3.Connection,
        items: list[SearchOutcome | SearchRouteOutcome | _Control],
    ) -> None:
        attempt_rows: list[tuple[object, ...]] = []
        route_rows: list[tuple[object, ...]] = []
        for item in items:
            if isinstance(item, _Control):
                self._insert_rows(connection, attempt_rows, route_rows)
                attempt_rows = []
                route_rows = []
                if item.clear:
                    item.deleted = self._clear_rows(connection)
                item.done.set()
            elif isinstance(item, SearchRouteOutcome):
                route_rows.append(_route_row_tuple(item))
            else:
                attempt_rows.append(_row_tuple(item))
        self._insert_rows(connection, attempt_rows, route_rows)

    def _insert_rows(
        self,
        connection: sqlite3.Connection,
        attempt_rows: list[tuple[object, ...]],
        route_rows: list[tuple[object, ...]],
    ) -> None:
        if not attempt_rows and not route_rows:
            return
        try:
            if attempt_rows:
                connection.executemany(_INSERT_SQL, attempt_rows)
            if route_rows:
                connection.executemany(_INSERT_ROUTE_SQL, route_rows)
            connection.commit()
        except Exception:
            connection.rollback()
            logger.exception(
                "websearch analytics insert failed; dropping {} attempt(s) and"
                " {} route(s)",
                len(attempt_rows),
                len(route_rows),
            )
            return
        self._inserts_since_prune += len(attempt_rows) + len(route_rows)
        if self._inserts_since_prune >= self._prune_every:
            self._inserts_since_prune = 0
            self._prune(connection)

    def _clear_rows(self, connection: sqlite3.Connection) -> int:
        cursor = connection.execute("DELETE FROM search_log")
        connection.execute("DELETE FROM search_route_log")
        connection.commit()
        return cursor.rowcount

    def _prune(self, connection: sqlite3.Connection) -> None:
        try:
            connection.execute(_PRUNE_SQL, (self._max_rows,))
            connection.execute(_PRUNE_ROUTES_SQL, (self._max_rows,))
            connection.commit()
        except Exception:
            connection.rollback()
            logger.exception("websearch analytics retention prune failed")

    def _connect_reader(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        _initialize_schema(connection)
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


def record_search_route(outcome: SearchRouteOutcome) -> None:
    """Persist one logical route outcome unless analytics is disabled."""

    try:
        if not _log_enabled():
            return
        get_shared_store().record_route(outcome)
    except Exception:
        logger.exception("failed to record web search route")


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
        outcome.route_id,
        max(1, outcome.attempt_number),
    )


def _route_row_tuple(outcome: SearchRouteOutcome) -> tuple[object, ...]:
    return (
        outcome.route_id,
        outcome.ts_epoch,
        outcome.ts_iso,
        outcome.query[:QUERY_LOG_CHARS],
        outcome.primary_provider,
        outcome.terminal_provider,
        _encode_provider_path(outcome.provider_path),
        max(0, outcome.attempt_count),
        int(outcome.fallback_used),
        outcome.duration_ms,
        outcome.status,
        outcome.results_count,
        outcome.cost_usd,
        outcome.error_kind,
        (
            outcome.error_message[:ERROR_MESSAGE_LOG_CHARS]
            if outcome.error_message
            else None
        ),
    )


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(zip(row.keys(), row, strict=True))


def _attempt_filter_where(
    *,
    provider: str | None,
    status: str | None,
    q: str | None,
    since_epoch: float | None,
    until_epoch: float | None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if provider:
        clauses.append("provider = ?")
        params.append(provider)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if q:
        clauses.append("instr(lower(query), lower(?)) > 0")
        params.append(q)
    if since_epoch is not None:
        clauses.append("ts_epoch >= ?")
        params.append(since_epoch)
    if until_epoch is not None:
        clauses.append("ts_epoch <= ?")
        params.append(until_epoch)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def _route_filter_where(
    *,
    provider: str | None,
    status: str | None,
    q: str | None,
    since_epoch: float | None,
    until_epoch: float | None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if provider:
        clauses.append("instr(provider_path, '|' || ? || '|') > 0")
        params.append(provider)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if q:
        clauses.append("instr(lower(query), lower(?)) > 0")
        params.append(q)
    if since_epoch is not None:
        clauses.append("ts_epoch >= ?")
        params.append(since_epoch)
    if until_epoch is not None:
        clauses.append("ts_epoch <= ?")
        params.append(until_epoch)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def _attempt_stats(
    connection: sqlite3.Connection,
    where: str,
    params: list[Any],
    period: str,
) -> dict[str, Any]:
    totals = _row_dict(
        connection.execute(
            "SELECT COUNT(*) AS requests,"
            " COALESCE(SUM(status = 'error'), 0) AS errors,"
            " AVG(duration_ms) AS avg_duration_ms,"
            " COALESCE(SUM(results_count), 0) AS results,"
            " SUM(cost_usd) AS cost_usd"
            f" FROM search_log {where}",
            params,
        ).fetchone()
    )
    by_provider = [
        _shaped_aggregate(row)
        for row in connection.execute(
            "SELECT provider, COUNT(*) AS requests,"
            " COALESCE(SUM(status = 'error'), 0) AS errors,"
            " AVG(duration_ms) AS avg_duration_ms,"
            " COALESCE(SUM(results_count), 0) AS results,"
            " SUM(cost_usd) AS cost_usd"
            f" FROM search_log {where} GROUP BY provider"
            " ORDER BY requests DESC, provider ASC",
            params,
        ).fetchall()
    ]
    by_key = [
        _shaped_aggregate(row)
        for row in connection.execute(
            "SELECT provider, key_label, COUNT(*) AS requests,"
            " COALESCE(SUM(status = 'error'), 0) AS errors,"
            " AVG(duration_ms) AS avg_duration_ms,"
            " COALESCE(SUM(results_count), 0) AS results"
            f" FROM search_log {where} GROUP BY provider, key_label"
            " ORDER BY requests DESC, provider ASC, key_label ASC",
            params,
        ).fetchall()
    ]
    error_where = f"{where} {'AND' if where else 'WHERE'} status = 'error'"
    top_errors = [
        _row_dict(row)
        for row in connection.execute(
            "SELECT error_kind, error_message, COUNT(*) AS count"
            f" FROM search_log {error_where}"
            " GROUP BY error_kind, error_message"
            " ORDER BY count DESC, error_kind ASC, error_message ASC"
            " LIMIT ?",
            (*params, _TOP_ERRORS_LIMIT),
        ).fetchall()
    ]
    series_rows = connection.execute(
        f"SELECT ts_epoch, provider, status, results_count FROM search_log {where}",
        params,
    ).fetchall()
    bounds = _row_dict(
        connection.execute(
            f"SELECT MIN(ts_epoch) AS first_ts_epoch,"
            f" MAX(ts_epoch) AS last_ts_epoch FROM search_log {where}",
            params,
        ).fetchone()
    )
    requests_total = int(totals["requests"])
    errors_total = int(totals["errors"])
    return {
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
        "window": {
            "since_epoch": bounds["first_ts_epoch"],
            "until_epoch": bounds["last_ts_epoch"],
        },
    }


def _route_stats(
    connection: sqlite3.Connection,
    where: str,
    params: list[Any],
    period: str,
) -> dict[str, Any]:
    totals = _row_dict(
        connection.execute(
            "SELECT COUNT(*) AS routes,"
            " COALESCE(SUM(status = 'error'), 0) AS errors,"
            " COALESCE(SUM(fallback_used), 0) AS fallbacks,"
            " AVG(attempt_count) AS avg_attempts,"
            " AVG(duration_ms) AS avg_duration_ms,"
            " COALESCE(SUM(results_count), 0) AS results,"
            " SUM(cost_usd) AS cost_usd"
            f" FROM search_route_log {where}",
            params,
        ).fetchone()
    )
    by_primary_provider = _route_breakdown(
        connection, "primary_provider", where, params
    )
    by_terminal_provider = _route_breakdown(
        connection, "terminal_provider", where, params
    )
    error_where = f"{where} {'AND' if where else 'WHERE'} status = 'error'"
    top_errors = [
        _row_dict(row)
        for row in connection.execute(
            "SELECT error_kind, error_message, COUNT(*) AS count"
            f" FROM search_route_log {error_where}"
            " GROUP BY error_kind, error_message"
            " ORDER BY count DESC, error_kind ASC, error_message ASC"
            " LIMIT ?",
            (*params, _TOP_ERRORS_LIMIT),
        ).fetchall()
    ]
    series_rows = connection.execute(
        "SELECT ts_epoch, terminal_provider AS provider, status, fallback_used,"
        f" results_count FROM search_route_log {where}",
        params,
    ).fetchall()
    bounds = _row_dict(
        connection.execute(
            f"SELECT MIN(ts_epoch) AS first_ts_epoch,"
            f" MAX(ts_epoch) AS last_ts_epoch FROM search_route_log {where}",
            params,
        ).fetchone()
    )
    last_row = connection.execute(
        f"SELECT {_ROUTE_COLUMNS} FROM search_route_log {where}"
        " ORDER BY ts_epoch DESC, id DESC LIMIT 1",
        params,
    ).fetchone()
    routes_total = int(totals["routes"])
    errors_total = int(totals["errors"])
    fallbacks_total = int(totals["fallbacks"])
    return {
        "totals": {
            "searches": routes_total,
            "successes": routes_total - errors_total,
            "errors": errors_total,
            "fallbacks": fallbacks_total,
            "fallback_rate": (
                round(fallbacks_total / routes_total, 6) if routes_total else 0.0
            ),
            "avg_attempts": _rounded(totals["avg_attempts"]),
            "avg_duration_ms": _rounded(totals["avg_duration_ms"]),
            "results": int(totals["results"]),
            "cost_usd": totals["cost_usd"],
        },
        "by_primary_provider": by_primary_provider,
        "by_terminal_provider": by_terminal_provider,
        "series": _route_series(series_rows, period),
        "top_errors": top_errors,
        "last_route": _route_dict(last_row) if last_row is not None else None,
        "window": {
            "since_epoch": bounds["first_ts_epoch"],
            "until_epoch": bounds["last_ts_epoch"],
        },
    }


def _route_breakdown(
    connection: sqlite3.Connection,
    column: str,
    where: str,
    params: list[Any],
) -> list[dict[str, Any]]:
    return [
        _shaped_route_aggregate(row)
        for row in connection.execute(
            f"SELECT {column} AS provider, COUNT(*) AS searches,"
            " COALESCE(SUM(status = 'error'), 0) AS errors,"
            " COALESCE(SUM(fallback_used), 0) AS fallbacks,"
            " AVG(duration_ms) AS avg_duration_ms,"
            " COALESCE(SUM(results_count), 0) AS results,"
            " SUM(cost_usd) AS cost_usd"
            f" FROM search_route_log {where} GROUP BY {column}"
            " ORDER BY searches DESC, provider ASC",
            params,
        ).fetchall()
    ]


def _shaped_route_aggregate(row: sqlite3.Row) -> dict[str, Any]:
    shaped = _row_dict(row)
    shaped["searches"] = int(shaped["searches"])
    shaped["errors"] = int(shaped["errors"])
    shaped["fallbacks"] = int(shaped["fallbacks"])
    shaped["avg_duration_ms"] = _rounded(shaped["avg_duration_ms"])
    shaped["results"] = int(shaped["results"])
    return shaped


def _route_dict(row: sqlite3.Row) -> dict[str, Any]:
    shaped = _row_dict(row)
    shaped["providers"] = _decode_provider_path(str(shaped.pop("provider_path")))
    shaped["fallback_used"] = bool(shaped["fallback_used"])
    return shaped


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
        bucket = _time_bucket(float(row["ts_epoch"]), period)
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


def _route_series(rows: list[sqlite3.Row], period: str) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        bucket = _time_bucket(float(row["ts_epoch"]), period)
        key = (bucket, row["provider"])
        entry = buckets.setdefault(
            key,
            {
                "bucket": bucket,
                "provider": row["provider"],
                "searches": 0,
                "errors": 0,
                "fallbacks": 0,
                "results": 0,
            },
        )
        entry["searches"] += 1
        entry["errors"] += 1 if row["status"] == "error" else 0
        entry["fallbacks"] += int(row["fallback_used"])
        entry["results"] += row["results_count"]
    return sorted(
        buckets.values(), key=lambda entry: (entry["bucket"], entry["provider"])
    )


def _time_bucket(ts_epoch: float, period: str) -> str:
    moment = datetime.fromtimestamp(ts_epoch, tz=UTC)
    if period == "hourly":
        return moment.strftime("%Y-%m-%dT%H:00")
    if period == "daily":
        return moment.strftime("%Y-%m-%d")
    if period == "weekly":
        iso = moment.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return f"{moment.year}-{moment.month:02d}"


def _encode_provider_path(providers: tuple[str, ...]) -> str:
    return f"|{'|'.join(providers)}|"


def _decode_provider_path(encoded: str) -> list[str]:
    return [provider for provider in encoded.split("|") if provider]


def _initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(_SCHEMA)
    _ensure_column(
        connection,
        "search_log",
        "route_id",
        "ALTER TABLE search_log ADD COLUMN route_id TEXT",
    )
    _ensure_column(
        connection,
        "search_log",
        "attempt_number",
        "ALTER TABLE search_log ADD COLUMN attempt_number INTEGER NOT NULL DEFAULT 1",
    )
    connection.executescript(_POST_MIGRATION_SCHEMA)
    connection.commit()


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    alter_sql: str,
) -> None:
    columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    if column in columns:
        return
    try:
        connection.execute(alter_sql)
    except sqlite3.OperationalError:
        # A concurrent reader/writer initialization may have won the migration race.
        columns = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            raise


def _rounded(value: Any) -> float | None:
    return round(float(value), 3) if value is not None else None
