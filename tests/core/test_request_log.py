"""Unit tests for the SQLite request log store."""

import gc
import sqlite3
import time
from typing import Any

import pytest

from free_claude_code.core.request_log import (
    LIST_BODY_PREVIEW_CHARS,
    MAX_ERROR_CHARS,
    MAX_TEXT_CHARS,
    RequestLogStore,
    RequestRecord,
    get_request_log_store,
    reset_request_log_stores,
)


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "requests.db", max_rows=100)
    yield store
    store.close()


def _record(request_id: str, **overrides) -> RequestRecord:
    defaults: dict[str, Any] = {
        "id": request_id,
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "requested_model": "claude-sonnet-4-5",
        "provider": "nvidia_nim",
        "resolved_model": "test-model",
        "stream": True,
        "input_text": "hello",
        "output_text": "world",
        "tokens_in": 10,
        "tokens_out": 20,
        "ttft_ms": 12.5,
        "duration_ms": 120.0,
        "status": "success",
    }
    defaults.update(overrides)
    return RequestRecord(**defaults)


def test_enqueue_persists_record(store: RequestLogStore) -> None:
    store.enqueue(_record("r1"))
    store.close()
    row = store.get_request("r1")
    assert row is not None
    assert row["provider"] == "nvidia_nim"
    assert row["stream"] is True
    assert row["tokens_in"] == 10
    assert row["params"] is None
    assert row["ts_iso"].endswith("+00:00")


def test_close_flushes_and_is_idempotent(store: RequestLogStore) -> None:
    store.enqueue(_record("r1"))
    store.close()
    store.close()
    _, total = store.list_requests()
    assert total == 1


def test_list_paging_and_order(store: RequestLogStore) -> None:
    base = time.time()
    for index in range(5):
        store.enqueue(_record(f"r{index}", ts_epoch=base + index))
    store.close()
    rows, total = store.list_requests(limit=2, offset=0)
    assert total == 5
    assert [row["id"] for row in rows] == ["r4", "r3"]
    rows, _ = store.list_requests(limit=2, offset=4)
    assert [row["id"] for row in rows] == ["r0"]


def test_list_filters(store: RequestLogStore) -> None:
    base = time.time()
    store.enqueue(
        _record(
            "a", provider="p1", resolved_model="m1", status="success", ts_epoch=base
        )
    )
    store.enqueue(
        _record(
            "b", provider="p2", resolved_model="m2", status="error", ts_epoch=base + 10
        )
    )
    store.enqueue(
        _record(
            "c",
            provider="p1",
            resolved_model="m2",
            status="cancelled",
            endpoint="/v1/responses",
            ts_epoch=base + 20,
        )
    )
    store.close()
    rows, total = store.list_requests(provider="p1")
    assert total == 2
    assert {row["id"] for row in rows} == {"a", "c"}
    _, total = store.list_requests(model="m2")
    assert total == 2
    _, total = store.list_requests(status="error")
    assert total == 1
    _, total = store.list_requests(endpoint="/v1/responses")
    assert total == 1
    _, total = store.list_requests(since=base + 5, until=base + 15)
    assert total == 1


def test_list_text_search(store: RequestLogStore) -> None:
    store.enqueue(
        _record("a", input_text="deploy the kubernetes cluster", output_text="done")
    )
    store.enqueue(_record("b", input_text="hello", output_text="kubernetes is complex"))
    store.enqueue(_record("c", input_text="hello", output_text="world"))
    store.close()
    rows, total = store.list_requests(q="kubernetes")
    assert total == 2
    assert {row["id"] for row in rows} == {"a", "b"}
    _, total = store.list_requests(q="KUBERNETES")
    assert total == 2  # SQLite LIKE is case-insensitive for ASCII
    _, total = store.list_requests(q="missing-text")
    assert total == 0
    _, total = store.list_requests(q="hello", provider="nvidia_nim")
    assert total == 2  # matches b and c, combined with the provider filter


def test_list_truncates_bodies_but_get_returns_full(store: RequestLogStore) -> None:
    long_text = "x" * (LIST_BODY_PREVIEW_CHARS + 100)
    store.enqueue(_record("r1", input_text=long_text, output_text=long_text))
    store.close()
    rows, _ = store.list_requests()
    assert len(rows[0]["input_text"]) == LIST_BODY_PREVIEW_CHARS
    assert rows[0]["input_text_truncated"] is True
    full = store.get_request("r1")
    assert full is not None
    assert len(full["input_text"]) == LIST_BODY_PREVIEW_CHARS + 100
    assert full["input_text_truncated"] is False


def test_get_missing_returns_none(store: RequestLogStore) -> None:
    assert store.get_request("nope") is None


def test_stats_aggregates(store: RequestLogStore) -> None:
    base = time.time()
    store.enqueue(
        _record("s1", ts_epoch=base, duration_ms=100.0, tokens_in=5, tokens_out=7)
    )
    store.enqueue(
        _record(
            "s2",
            ts_epoch=base + 3600,
            duration_ms=300.0,
            status="error",
            error_kind="rate_limit",
            error_message="slow down",
            tokens_in=15,
            tokens_out=1,
        )
    )
    store.enqueue(
        _record(
            "s3",
            ts_epoch=base + 7200,
            duration_ms=None,
            status="cancelled",
            tokens_in=None,
            tokens_out=None,
        )
    )
    store.close()
    stats = store.stats()
    assert stats["total"] == 3
    assert stats["success"] == 1
    assert stats["error"] == 1
    assert stats["cancelled"] == 1
    assert stats["error_rate"] == pytest.approx(1 / 3)
    assert stats["tokens_in"] == 20
    assert stats["tokens_out"] == 8
    assert stats["avg_duration_ms"] == pytest.approx(200.0)
    assert stats["p50_duration_ms"] == pytest.approx(200.0)
    assert stats["p95_duration_ms"] == pytest.approx(290.0)
    assert stats["by_provider"][0]["key"] == "nvidia_nim"
    assert stats["by_provider"][0]["requests"] == 3
    assert stats["by_provider"][0]["errors"] == 1
    assert stats["by_model"][0]["tokens_out"] == 8
    assert stats["top_errors"] == [{"message": "slow down", "count": 1}]
    # 2h window -> hourly buckets
    assert len(stats["series"]) == 3
    assert "T" in stats["series"][0]["bucket"]


def test_stats_window_filter(store: RequestLogStore) -> None:
    base = time.time()
    store.enqueue(_record("old", ts_epoch=base - 3 * 86400))
    store.enqueue(_record("new", ts_epoch=base))
    store.close()
    stats = store.stats(since=base - 10)
    assert stats["total"] == 1
    daily = store.stats()
    assert daily["total"] == 2
    assert all("T" not in point["bucket"] for point in daily["series"])


def test_stats_applies_all_list_filters_to_every_aggregate(
    store: RequestLogStore,
) -> None:
    base = time.time()
    store.enqueue(
        _record(
            "match-error",
            provider="selected",
            requested_model="requested-match",
            resolved_model="resolved-other",
            endpoint="/v1/responses",
            ts_epoch=base,
            status="error",
            input_text="needle in input",
            output_text="ignored",
            tokens_in=7,
            tokens_out=3,
            duration_ms=40.0,
            ttft_ms=8.0,
            error_message="selected failure",
        )
    )
    store.enqueue(
        _record(
            "match-success",
            provider="selected",
            requested_model="requested-match",
            resolved_model="resolved-other",
            endpoint="/v1/responses",
            ts_epoch=base + 60,
            input_text="ignored",
            output_text="needle in output",
            tokens_in=11,
            tokens_out=5,
            duration_ms=80.0,
            ttft_ms=12.0,
        )
    )
    store.enqueue(
        _record(
            "wrong-provider",
            provider="other",
            requested_model="requested-match",
            endpoint="/v1/responses",
            ts_epoch=base + 120,
            status="error",
            input_text="needle",
            error_message="unselected failure",
        )
    )
    store.enqueue(
        _record(
            "wrong-model",
            provider="selected",
            requested_model="different",
            resolved_model="different",
            endpoint="/v1/responses",
            ts_epoch=base + 180,
            input_text="needle",
        )
    )
    store.enqueue(
        _record(
            "wrong-endpoint",
            provider="selected",
            requested_model="requested-match",
            endpoint="/v1/messages",
            ts_epoch=base + 240,
            input_text="needle",
        )
    )
    store.enqueue(
        _record(
            "outside-window",
            provider="selected",
            requested_model="requested-match",
            endpoint="/v1/responses",
            ts_epoch=base + 3600,
            input_text="needle",
        )
    )
    store.close()

    stats = store.stats(
        provider="selected",
        model="requested-match",
        endpoint="/v1/responses",
        since=base - 1,
        until=base + 300,
        q="needle",
    )

    assert stats["total"] == 2
    assert stats["success"] == 1
    assert stats["error"] == 1
    assert stats["cancelled"] == 0
    assert stats["error_rate"] == pytest.approx(0.5)
    assert stats["tokens_in"] == 18
    assert stats["tokens_out"] == 8
    assert stats["avg_duration_ms"] == pytest.approx(60.0)
    assert stats["p50_duration_ms"] == pytest.approx(60.0)
    assert stats["p95_duration_ms"] == pytest.approx(78.0)
    assert stats["avg_ttft_ms"] == pytest.approx(10.0)
    assert stats["by_provider"] == [
        {
            "key": "selected",
            "requests": 2,
            "tokens_in": 18,
            "tokens_out": 8,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "cache_reported": 0,
            "errors": 1,
            "avg_duration_ms": 60.0,
        }
    ]
    assert stats["by_model"] == [
        {
            "key": "resolved-other",
            "requests": 2,
            "tokens_in": 18,
            "tokens_out": 8,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "cache_reported": 0,
            "errors": 1,
            "avg_duration_ms": 60.0,
        }
    ]
    assert stats["top_errors"] == [{"message": "selected failure", "count": 1}]
    assert sum(point["requests"] for point in stats["series"]) == 2
    assert sum(point["tokens"] for point in stats["series"]) == 26
    assert sum(point["errors"] for point in stats["series"]) == 1


def test_stats_status_filter_changes_cards_breakdowns_errors_and_series(
    store: RequestLogStore,
) -> None:
    base = time.time()
    store.enqueue(_record("success", ts_epoch=base, duration_ms=10.0))
    store.enqueue(
        _record(
            "error",
            ts_epoch=base + 1,
            status="error",
            duration_ms=90.0,
            error_message="boom",
        )
    )
    store.close()

    stats = store.stats(status="success")

    assert stats["total"] == 1
    assert stats["success"] == 1
    assert stats["error"] == 0
    assert stats["error_rate"] == 0.0
    assert stats["p50_duration_ms"] == 10.0
    assert stats["by_provider"][0]["requests"] == 1
    assert stats["by_provider"][0]["errors"] == 0
    assert stats["top_errors"] == []
    assert stats["series"][0]["requests"] == 1
    assert stats["series"][0]["errors"] == 0


def test_prune_keeps_newest(tmp_path) -> None:
    store = RequestLogStore(tmp_path / "requests.db", max_rows=3)
    base = time.time()
    for index in range(6):
        store.enqueue(_record(f"r{index}", ts_epoch=base + index))
    store.close()
    deleted = store.prune()
    assert deleted == 3
    rows, total = store.list_requests(limit=10)
    assert total == 3
    assert [row["id"] for row in rows] == ["r5", "r4", "r3"]
    store.close()


def test_clear(store: RequestLogStore) -> None:
    store.enqueue(_record("r1"))
    store.close()
    assert store.clear() == 1
    _, total = store.list_requests()
    assert total == 0


def test_error_message_capped(store: RequestLogStore) -> None:
    store.enqueue(_record("r1", status="error", error_message="e" * 5000))
    store.close()
    row = store.get_request("r1")
    assert row is not None
    assert len(row["error_message"]) == 2000


def _live_connection_count() -> int:
    return sum(1 for obj in gc.get_objects() if isinstance(obj, sqlite3.Connection))


def test_enqueue_caps_bodies_before_queueing(store: RequestLogStore) -> None:
    """Oversized bodies must be capped before the record reaches the queue."""
    record = _record(
        "r1",
        input_text="i" * (MAX_TEXT_CHARS + 500),
        output_text="o" * (MAX_TEXT_CHARS + 500),
        status="error",
        error_message="e" * (MAX_ERROR_CHARS + 500),
    )
    store.enqueue(record)
    # ``enqueue`` caps in place, so the queued object itself is already bounded
    # rather than holding the full body until the writer flushes it.
    assert record.input_text is not None
    assert record.output_text is not None
    assert record.error_message is not None
    assert len(record.input_text) == MAX_TEXT_CHARS
    assert len(record.output_text) == MAX_TEXT_CHARS
    assert len(record.error_message) == MAX_ERROR_CHARS


def test_read_paths_close_connections(store: RequestLogStore) -> None:
    """Read paths must not accumulate connections between GC passes."""
    store.enqueue(_record("r1"))
    store.close()
    gc.collect()
    gc.disable()
    try:
        before = _live_connection_count()
        for _ in range(25):
            store.list_requests()
            store.get_request("r1")
        after = _live_connection_count()
    finally:
        gc.enable()
    assert after == before


def _auto_vacuum_mode(path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("PRAGMA auto_vacuum").fetchone()[0])
    finally:
        conn.close()


def test_auto_vacuum_becomes_incremental(tmp_path) -> None:
    """The store must end up on incremental auto-vacuum.

    A populated database is converted by the writer thread rather than during
    construction, so poll instead of asserting immediately.
    """
    store = RequestLogStore(tmp_path / "requests.db", max_rows=10)
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if _auto_vacuum_mode(store.db_path) == 2:
                break
            time.sleep(0.05)
        assert _auto_vacuum_mode(store.db_path) == 2
    finally:
        store.close()


def test_stats_covering_index_is_created(tmp_path) -> None:
    """Aggregates must be able to run index-only, without touching bodies."""
    store = RequestLogStore(tmp_path / "requests.db", max_rows=10)
    try:
        deadline = time.monotonic() + 10.0
        plan: list[str] = []
        while time.monotonic() < deadline:
            conn = sqlite3.connect(store.db_path)
            try:
                names = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )
                }
                if "idx_requests_stats_v3" in names:
                    plan = [
                        str(row[3])
                        for row in conn.execute(
                            "EXPLAIN QUERY PLAN SELECT COUNT(*),"
                            " AVG(duration_ms) FROM requests"
                        )
                    ]
                    break
            finally:
                conn.close()
            time.sleep(0.05)
        assert plan, "covering index was never created"
        assert any("idx_requests_stats_v3" in step for step in plan), plan
    finally:
        store.close()


def test_construction_does_not_block_on_vacuum(tmp_path) -> None:
    """Converting a large database must not happen on the caller's thread."""
    path = tmp_path / "requests.db"
    seed = RequestLogStore(path, max_rows=50_000)
    body = "x" * 20_000
    for index in range(300):
        seed.enqueue(_record(f"s{index}", input_text=body, output_text=body))
    seed.close()
    # Force the legacy (non-incremental) layout the conversion has to fix.
    conn = sqlite3.connect(path)
    try:
        conn.isolation_level = None
        conn.execute("PRAGMA auto_vacuum=NONE")
        conn.execute("VACUUM")
    finally:
        conn.close()
    assert _auto_vacuum_mode(path) == 0

    started = time.perf_counter()
    store = RequestLogStore(path, max_rows=50_000)
    construction_seconds = time.perf_counter() - started
    try:
        assert construction_seconds < 1.0
    finally:
        store.close()


def test_prune_reclaims_file_space(tmp_path) -> None:
    """Repeated insert/prune cycles must not grow the file without bound."""
    store = RequestLogStore(tmp_path / "requests.db", max_rows=10)
    try:
        body = "x" * 10_000
        sizes = []
        for cycle in range(4):
            for index in range(40):
                store.enqueue(
                    _record(f"c{cycle}-{index}", input_text=body, output_text=body)
                )
            store.prune()
            sizes.append(store.db_path.stat().st_size)
    finally:
        store.close()
    # Later cycles must not keep ratcheting the file upward.
    assert sizes[-1] <= sizes[0] * 2


def test_percentiles_on_empty_table_are_none(store: RequestLogStore) -> None:
    """No rows at all must not error the new rank-lookup path."""
    stats = store.stats()
    assert stats["total"] == 0
    assert stats["p50_duration_ms"] is None
    assert stats["p95_duration_ms"] is None


def test_percentiles_ignore_rows_without_duration(store: RequestLogStore) -> None:
    """Rows with duration_ms IS NULL must not shift the rank computation."""
    store.enqueue(_record("no-duration", duration_ms=None))
    store.enqueue(_record("has-duration", duration_ms=42.0))
    store.close()
    stats = store.stats()
    assert stats["total"] == 2
    assert stats["p50_duration_ms"] == pytest.approx(42.0)
    assert stats["p95_duration_ms"] == pytest.approx(42.0)


def test_percentiles_match_old_interpolation_unfiltered_and_filtered(
    store: RequestLogStore,
) -> None:
    """Pin the adaptive index-seek (unfiltered) vs single-sort (filtered) paths.

    Expected values are hand-computed with the same formula the removed
    ``_percentile`` used: ``position = fraction * (n - 1)``, interpolating
    between the floor and ceiling ranks.
    """
    # provider "a": durations 10, 50, 90 (n=3) -> p50 index 1.0 = 50;
    #   p95 position 1.9 interpolates rank1=50 and rank2=90: 50+40*0.9=86.0
    store.enqueue(_record("a1", provider="a", duration_ms=10.0))
    store.enqueue(_record("a2", provider="a", duration_ms=90.0))
    store.enqueue(_record("a3", provider="a", duration_ms=50.0))
    # provider "b": durations 20, 40 -- combine with "a" for the unfiltered set.
    store.enqueue(_record("b1", provider="b", duration_ms=20.0))
    store.enqueue(_record("b2", provider="b", duration_ms=40.0))
    store.close()

    # Unfiltered: combined sorted durations are 10, 20, 40, 50, 90 (n=5).
    # p50 position 2.0 = index 2 = 40; p95 position 3.8 interpolates
    # rank3=50 and rank4=90: 50+40*0.8=82.0. This path uses the index-seek
    # branch of ``_percentiles`` (no WHERE clause).
    unfiltered = store.stats()
    assert unfiltered["p50_duration_ms"] == pytest.approx(40.0)
    assert unfiltered["p95_duration_ms"] == pytest.approx(82.0)

    # Filtered: this path uses the single-sort branch of ``_percentiles``
    # (a WHERE clause is present), which must still match the same formula.
    filtered = store.stats(provider="a")
    assert filtered["p50_duration_ms"] == pytest.approx(50.0)
    assert filtered["p95_duration_ms"] == pytest.approx(86.0)


def test_stats_cache_evicts_least_recently_used(tmp_path) -> None:
    """The stats cache must be bounded rather than growing without limit."""
    from free_claude_code.core import request_log as request_log_module

    store = RequestLogStore(tmp_path / "requests.db", max_rows=100)
    try:
        store.enqueue(_record("r1"))
        store.close()
        max_entries = request_log_module._STATS_CACHE_MAX_ENTRIES
        # Fill the cache with distinct filter combinations, one past capacity.
        for index in range(max_entries + 1):
            store.stats(provider=f"provider-{index}")
        with store._stats_lock:
            assert len(store._stats_cache) == max_entries
            # The oldest key (provider-0) was evicted; the newest survives.
            assert ("provider-0", None, None, None, None, None, None, None) not in (
                store._stats_cache
            )
            assert (
                f"provider-{max_entries}",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            ) in store._stats_cache
    finally:
        store.close()


def test_breakdown_truncation_flag(tmp_path) -> None:
    """A breakdown beyond the cap must be truncated with a visible flag."""
    from free_claude_code.core import request_log as request_log_module

    store = RequestLogStore(tmp_path / "requests.db", max_rows=1000)
    try:
        limit = request_log_module._BREAKDOWN_LIMIT
        for index in range(limit + 5):
            store.enqueue(_record(f"r{index}", provider=f"provider-{index}"))
        store.close()
        stats = store.stats()
        assert len(stats["by_provider"]) == limit
        assert stats["by_provider_truncated"] is True
        # Untruncated breakdowns still report the flag as False, not absent.
        assert stats["by_model_truncated"] is False
    finally:
        store.close()


def test_pulse_reports_total_and_last_ts(store: RequestLogStore) -> None:
    base = time.time()
    store.enqueue(_record("r1", ts_epoch=base))
    store.enqueue(_record("r2", ts_epoch=base + 10))
    store.close()
    pulse = store.pulse()
    assert pulse["total"] == 2
    assert pulse["last_ts"] == pytest.approx(base + 10)


def test_pulse_on_empty_table(store: RequestLogStore) -> None:
    pulse = store.pulse()
    assert pulse == {"total": 0, "last_ts": None}


def test_pulse_applies_filters(store: RequestLogStore) -> None:
    store.enqueue(_record("a", provider="p1"))
    store.enqueue(_record("b", provider="p2"))
    store.close()
    assert store.pulse(provider="p1")["total"] == 1
    assert store.pulse(provider="missing")["total"] == 0


def test_stats_are_cached_within_ttl(store: RequestLogStore) -> None:
    store.enqueue(_record("r1"))
    store.close()
    first = store.stats()
    assert first["total"] == 1
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT INTO requests (id, ts_epoch, ts_iso, endpoint, protocol, status)"
            " VALUES ('r2', ?, '2024-01-01T00:00:00+00:00', '/v1/messages',"
            " 'anthropic', 'success')",
            (time.time(),),
        )
    assert store.stats()["total"] == 1  # served from the short-lived cache
    # Mutating a returned payload must not corrupt the cached copy.
    store.stats()["enabled"] = True
    assert "enabled" not in store.stats()


def test_shared_store_registry(tmp_path) -> None:
    path = tmp_path / "shared.db"
    first = get_request_log_store(path)
    assert get_request_log_store(path) is first
    assert get_request_log_store(path, enabled=False) is None
    reset_request_log_stores()
    assert get_request_log_store(path) is not first
    reset_request_log_stores()


_OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY, ts_epoch REAL NOT NULL, ts_iso TEXT NOT NULL,
    endpoint TEXT NOT NULL, protocol TEXT NOT NULL, requested_model TEXT,
    provider TEXT, resolved_model TEXT, stream INTEGER NOT NULL DEFAULT 0,
    input_text TEXT, output_text TEXT, input_sha256 TEXT, output_sha256 TEXT,
    input_chars INTEGER, output_chars INTEGER, reasoning TEXT, params TEXT,
    tokens_in INTEGER, tokens_out INTEGER, ttft_ms REAL, duration_ms REAL,
    status TEXT NOT NULL, error_kind TEXT, error_message TEXT, headers TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_stats ON requests(
    ts_epoch, status, provider, resolved_model, endpoint,
    requested_model, duration_ms, ttft_ms, tokens_in, tokens_out);
"""


def _indexes(db_path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    finally:
        conn.close()


def test_key_attribution_round_trips(store: RequestLogStore) -> None:
    store.enqueue(_record("r1", key_index=1, key_label="abcd…wxyz"))
    store.close()
    row = store.get_request("r1")
    assert row is not None
    assert row["key_index"] == 1
    assert row["key_label"] == "abcd…wxyz"


def test_list_filters_and_aggregates_by_key(store: RequestLogStore) -> None:
    store.enqueue(_record("r1", key_index=0, key_label="aaaa…1111"))
    store.enqueue(_record("r2", key_index=0, key_label="aaaa…1111"))
    store.enqueue(_record("r3", key_index=1, key_label="bbbb…2222"))
    store.close()

    rows, total = store.list_requests(key="aaaa…1111")
    assert total == 2
    assert {row["id"] for row in rows} == {"r1", "r2"}
    assert all(row["key_label"] == "aaaa…1111" for row in rows)

    by_key = {entry["key"]: entry for entry in store.stats()["by_key"]}
    assert by_key["aaaa…1111"]["requests"] == 2
    assert by_key["bbbb…2222"]["requests"] == 1


def test_stats_key_filter_narrows_totals(store: RequestLogStore) -> None:
    store.enqueue(_record("r1", key_index=0, key_label="aaaa…1111"))
    store.enqueue(_record("r2", key_index=1, key_label="bbbb…2222"))
    store.close()
    assert store.stats()["total"] == 2
    assert store.stats(key="bbbb…2222")["total"] == 1


def test_migrates_a_database_created_before_key_columns(tmp_path) -> None:
    """An existing log must gain the key columns without losing its rows."""
    db_path = tmp_path / "requests.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_OLD_SCHEMA)
        conn.execute(
            "INSERT INTO requests (id, ts_epoch, ts_iso, endpoint, protocol,"
            " status, provider, tokens_in, tokens_out)"
            " VALUES ('legacy', ?, 'x', '/v1/messages', 'anthropic',"
            " 'success', 'nvidia_nim', 5, 7)",
            (time.time(),),
        )
        conn.commit()
    finally:
        conn.close()

    store = RequestLogStore(db_path, max_rows=100)
    try:
        store.enqueue(_record("fresh", key_index=0, key_label="cccc…3333"))
        store.close()

        legacy = store.get_request("legacy")
        assert legacy is not None
        assert legacy["key_label"] is None

        fresh = store.get_request("fresh")
        assert fresh is not None
        assert fresh["key_label"] == "cccc…3333"

        indexes = _indexes(db_path)
        assert "idx_requests_key" in indexes
        # The pre-existing covering index lacked key_label, so it must be
        # replaced rather than silently kept by CREATE INDEX IF NOT EXISTS.
        assert "idx_requests_stats" not in indexes
        assert "idx_requests_stats_v3" in indexes
    finally:
        store.close()


def test_key_breakdown_labels_unattributed_rows(store: RequestLogStore) -> None:
    store.enqueue(_record("r1"))
    store.close()
    by_key = {entry["key"] for entry in store.stats()["by_key"]}
    assert by_key == {"(unknown)"}


class TestCacheTokenAnalytics:
    """Cached prompt tokens are billed differently; they need their own columns."""

    def test_totals_and_breakdowns_report_cache_tokens(self, tmp_path) -> None:
        store = RequestLogStore(tmp_path / "requests.db")
        store.enqueue(
            _record(
                "a",
                provider="nvidia_nim",
                tokens_in=100,
                tokens_out=10,
                cache_read_tokens=900,
                cache_write_tokens=0,
            )
        )
        store.close()

        store = RequestLogStore(tmp_path / "requests.db")
        stats = store.stats()
        assert stats["cache_read_tokens"] == 900
        assert stats["cache_write_tokens"] == 0
        # tokens_in stays the *uncached* portion, matching Anthropic's usage
        # semantics -- summing it with cache reads would double count.
        assert stats["tokens_in"] == 100

        (provider,) = [r for r in stats["by_provider"] if r["key"] == "nvidia_nim"]
        assert provider["tokens_in"] == 100
        assert provider["tokens_out"] == 10
        assert provider["cache_read_tokens"] == 900
        store.close()

    def test_columns_are_added_to_a_database_created_before_them(
        self, tmp_path
    ) -> None:
        """Existing installs must migrate in place, not lose their history."""

        db_path = tmp_path / "requests.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(_OLD_SCHEMA)
            conn.execute(
                "INSERT INTO requests (id, ts_epoch, ts_iso, endpoint, protocol,"
                " status, provider, tokens_in, tokens_out)"
                " VALUES ('legacy', ?, 'x', '/v1/messages', 'anthropic',"
                " 'success', 'nvidia_nim', 5, 7)",
                (time.time(),),
            )
            conn.commit()
        finally:
            conn.close()

        store = RequestLogStore(db_path)
        store.enqueue(_record("fresh", provider="nvidia_nim", cache_read_tokens=7))
        store.close()

        with sqlite3.connect(db_path) as conn:
            rows = dict(
                conn.execute("SELECT id, cache_read_tokens FROM requests").fetchall()
            )
        assert rows["legacy"] is None  # pre-existing row survives, value unset
        assert rows["fresh"] == 7


def test_cache_reported_distinguishes_unsupported_from_zero(tmp_path) -> None:
    """A provider that never reports caching must not look like 0% caching."""

    store = RequestLogStore(tmp_path / "requests.db")
    store.enqueue(_record("silent", provider="nvidia_nim"))  # no cache fields
    store.enqueue(
        _record("reports", provider="deepseek", tokens_in=10, cache_read_tokens=0)
    )
    store.close()

    store = RequestLogStore(tmp_path / "requests.db")
    rows = {r["key"]: r for r in store.stats()["by_provider"]}
    # nvidia_nim said nothing about caching at all...
    assert rows["nvidia_nim"]["cache_reported"] == 0
    # ...whereas deepseek actively reported zero cached tokens.
    assert rows["deepseek"]["cache_reported"] == 1
    store.close()


def test_route_trace_round_trips_chain_attempt_and_diversion(
    store: RequestLogStore,
) -> None:
    """The whole routing decision, not just which model happened to answer.

    Nothing asserted any of this reaching storage before, which is how a vision
    diversion stayed invisible in the log for three releases: a diverted
    request looked identical to a route pointing at the adapter model.
    """
    store.enqueue(
        _record(
            "r_fallback",
            provider="opencode",
            resolved_model="deepseek-v4-flash-free",
            route_attempt=1,
            route_primary_model="nous_portal/tencent/hy3:free",
            route_chain=(
                "nous_portal/tencent/hy3:free,opencode/deepseek-v4-flash-free"
            ),
        )
    )
    store.enqueue(
        _record(
            "r_vision",
            provider="chatgpt_oauth",
            resolved_model="gpt-5.6-luna",
            route_attempt=0,
            route_chain="chatgpt_oauth/gpt-5.6-luna",
            route_diverted_from="nous_portal/tencent/hy3:free",
            route_diversion="vision",
        )
    )
    store.enqueue(_record("r_plain", route_attempt=0, route_chain="nvidia_nim/a"))
    store.close()

    fallback = store.get_request("r_fallback")
    assert fallback is not None
    assert fallback["route_attempt"] == 1
    assert fallback["route_chain"] == (
        "nous_portal/tencent/hy3:free,opencode/deepseek-v4-flash-free"
    )
    assert fallback["route_diversion"] is None

    vision = store.get_request("r_vision")
    assert vision is not None
    assert vision["route_diversion"] == "vision"
    assert vision["route_diverted_from"] == "nous_portal/tencent/hy3:free"
    assert vision["route_attempt"] == 0

    stats = store.stats()
    assert stats["served_by_fallback"] == 1
    assert stats["diverted"] == 1
    assert stats["fallback_routes"] == [
        {
            "primary": "nous_portal/tencent/hy3:free",
            "served_by": "opencode/deepseek-v4-flash-free",
            "count": 1,
        }
    ]
    assert stats["diverted_routes"] == [
        {
            "diverted_from": "nous_portal/tencent/hy3:free",
            "reason": "vision",
            "served_by": "chatgpt_oauth/gpt-5.6-luna",
            "count": 1,
        }
    ]


def test_route_trace_columns_are_added_to_a_pre_existing_database(tmp_path) -> None:
    """Live databases are 1.7 GB and predate every one of these columns."""
    path = tmp_path / "requests.db"
    seed = RequestLogStore(path, max_rows=100)
    seed.enqueue(_record("old"))
    seed.close()

    with sqlite3.connect(path) as conn:
        for column in ("route_chain", "route_diverted_from", "route_diversion"):
            conn.execute(f"ALTER TABLE requests DROP COLUMN {column}")

    reopened = RequestLogStore(path, max_rows=100)
    reopened.enqueue(_record("new", route_chain="a/b,c/d", route_diversion="vision"))
    reopened.close()

    old_row = reopened.get_request("old")
    new_row = reopened.get_request("new")
    assert old_row is not None and new_row is not None
    assert old_row["route_chain"] is None
    assert new_row["route_chain"] == "a/b,c/d"
