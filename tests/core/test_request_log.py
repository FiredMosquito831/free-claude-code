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


# --------------------------------------------------------- lifetime totals ---


def test_lifetime_totals_survive_the_retention_cap(tmp_path) -> None:
    """The bug this table exists for.

    Every figure in ``stats`` is a sum over ``requests``, which ``prune`` caps.
    Once the cap is reached one row leaves for each one that arrives, so those
    sums stop moving however much traffic runs. The all-time counters must not.
    """
    store = RequestLogStore(tmp_path / "requests.db", max_rows=3)
    base = time.time()
    for index in range(10):
        store.enqueue(_record(f"r{index}", ts_epoch=base + index))
    store.close()
    store.prune()

    windowed = store.stats()
    lifetime = store.lifetime()

    assert windowed["total"] == 3
    assert windowed["tokens_in"] == 30
    assert lifetime["requests"] == 10
    assert lifetime["tokens_in"] == 100
    assert lifetime["tokens_out"] == 200


def test_lifetime_breaks_down_by_provider_and_model(store: RequestLogStore) -> None:
    store.enqueue(_record("a", provider="nous_portal", resolved_model="hy3"))
    store.enqueue(_record("b", provider="nous_portal", resolved_model="hy3"))
    store.enqueue(_record("c", provider="open_router", resolved_model="other"))
    store.close()

    lifetime = store.lifetime()
    by_model = {row["name"]: row for row in lifetime["by_model"]}
    by_provider = {row["name"]: row for row in lifetime["by_provider"]}

    assert by_model["hy3"]["requests"] == 2
    assert by_model["hy3"]["tokens_in"] == 20
    assert by_provider["nous_portal"]["requests"] == 2
    assert by_provider["open_router"]["requests"] == 1


def test_lifetime_counts_statuses_fallbacks_and_diversions(
    store: RequestLogStore,
) -> None:
    store.enqueue(_record("ok"))
    store.enqueue(_record("bad", status="error"))
    store.enqueue(_record("gone", status="cancelled"))
    store.enqueue(_record("fell", route_attempt=2))
    store.enqueue(_record("saw", route_diversion="vision"))
    store.close()

    lifetime = store.lifetime()
    assert lifetime["requests"] == 5
    assert (lifetime["success"], lifetime["error"], lifetime["cancelled"]) == (3, 1, 1)
    assert lifetime["served_by_fallback"] == 1
    assert lifetime["diverted"] == 1


def test_lifetime_does_not_double_count_a_replayed_record(
    store: RequestLogStore,
) -> None:
    """The insert is ``INSERT OR REPLACE``; the counters are add-only."""
    store.enqueue(_record("same"))
    store.close()
    assert store.lifetime()["requests"] == 1

    reopened = RequestLogStore(store.db_path, max_rows=100)
    reopened.enqueue(_record("same", tokens_in=999))
    reopened.close()

    lifetime = reopened.lifetime()
    assert lifetime["requests"] == 1
    assert lifetime["tokens_in"] == 10


def test_lifetime_is_seeded_from_rows_written_before_the_upgrade(tmp_path) -> None:
    """Upgrading must not report zero all-time on a database full of history."""
    path = tmp_path / "requests.db"
    seed = RequestLogStore(path, max_rows=100)
    seed.enqueue(_record("old1"))
    seed.enqueue(_record("old2", status="error"))
    seed.close()

    # Reproduce a database written by a version that had no rollup at all.
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM request_totals")
        conn.execute("DELETE FROM request_log_meta")

    reopened = RequestLogStore(path, max_rows=100)
    reopened.close()

    lifetime = reopened.lifetime()
    assert lifetime["requests"] == 2
    assert lifetime["error"] == 1
    assert lifetime["tokens_in"] == 20


def test_backfill_runs_once_and_new_rows_still_count(tmp_path) -> None:
    path = tmp_path / "requests.db"
    seed = RequestLogStore(path, max_rows=100)
    seed.enqueue(_record("old"))
    seed.close()
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM request_totals")
        conn.execute("DELETE FROM request_log_meta")

    first = RequestLogStore(path, max_rows=100)
    first.enqueue(_record("new"))
    first.close()
    assert first.lifetime()["requests"] == 2

    # A second start must not re-seed the buckets it already wrote.
    second = RequestLogStore(path, max_rows=100)
    second.enqueue(_record("newer"))
    second.close()
    assert second.lifetime()["requests"] == 3


def test_clear_erases_the_lifetime_counters_too(store: RequestLogStore) -> None:
    store.enqueue(_record("r1"))
    store.close()
    assert store.lifetime()["requests"] == 1
    store.clear()
    assert store.lifetime()["requests"] == 0


def test_lifetime_on_an_empty_database_is_zero_not_null(store: RequestLogStore) -> None:
    lifetime = store.lifetime()
    assert lifetime["requests"] == 0
    assert lifetime["tokens_in"] == 0
    assert lifetime["first_day"] is None
    assert lifetime["by_model"] == []


# ------------------------------------------------------------- server uptime -


def test_coverage_records_a_session_for_a_running_store(tmp_path) -> None:
    """A quiet stretch is ambiguous unless uptime is recorded separately."""
    before = time.time()
    store = RequestLogStore(tmp_path / "requests.db", max_rows=100)
    store.enqueue(_record("r1"))
    store.close()

    coverage = store.coverage()
    assert len(coverage["sessions"]) == 1
    session = coverage["sessions"][0]
    assert session["started_at"] >= before
    assert session["last_seen_at"] >= session["started_at"]
    assert coverage["tracking_since"] is not None


def test_coverage_reports_nothing_before_tracking_began(tmp_path) -> None:
    store = RequestLogStore(tmp_path / "requests.db", max_rows=100)
    store.close()
    coverage = store.coverage(since=1.0, until=2.0)
    assert coverage["sessions"] == []
    assert coverage["covered_seconds"] == 0.0
    # Still set, so a caller can say "not recorded" rather than "down".
    assert coverage["tracking_since"] is not None


def test_coverage_merges_overlapping_sessions(tmp_path) -> None:
    """Two servers on one database must not add up to 200% uptime."""
    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=100)
    store.close()
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM server_sessions")
        conn.executemany(
            "INSERT INTO server_sessions (started_at, last_seen_at, pid)"
            " VALUES (?, ?, ?)",
            [(100.0, 200.0, 1), (150.0, 250.0, 2), (400.0, 500.0, 3)],
        )

    coverage = store.coverage()
    # 100->250 merged (150s) plus 400->500 (100s), not 100+100+100.
    assert coverage["covered_seconds"] == 250.0


def test_coverage_clips_sessions_to_the_requested_window(tmp_path) -> None:
    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=100)
    store.close()
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM server_sessions")
        conn.execute(
            "INSERT INTO server_sessions (started_at, last_seen_at, pid)"
            " VALUES (?, ?, ?)",
            (100.0, 300.0, 1),
        )

    assert store.coverage(since=200.0, until=250.0)["covered_seconds"] == 50.0
    assert store.coverage(since=250.0)["covered_seconds"] == 50.0


# ------------------------------------------------------- compressed bodies ---


def test_bodies_round_trip_through_compression(store: RequestLogStore) -> None:
    store.enqueue(
        _record(
            "r1",
            input_text="question " * 100,
            output_text="answer " * 100,
            thinking_text="pondering",
            tool_calls=[{"name": "Read", "input": {"path": "a.py"}}],
        )
    )
    store.close()

    row = store.get_request("r1")
    assert row is not None
    assert row["input_text"] == "question " * 100
    assert row["output_text"] == "answer " * 100
    assert row["thinking_text"] == "pondering"
    assert row["tool_calls"] == [{"name": "Read", "input": {"path": "a.py"}}]


def test_bodies_are_not_stored_inline_when_compressing(store: RequestLogStore) -> None:
    """The whole point: the text must leave the row it used to bloat."""
    store.enqueue(_record("r1", input_text="x" * 5000))
    store.close()

    with sqlite3.connect(store.db_path) as conn:
        inline = conn.execute(
            "SELECT input_text, output_text FROM requests WHERE id = 'r1'"
        ).fetchone()
        blobs = conn.execute("SELECT COUNT(*) FROM request_bodies").fetchone()[0]
    assert inline == (None, None)
    assert blobs == 1


def test_compression_actually_shrinks_repetitive_bodies(tmp_path) -> None:
    store = RequestLogStore(tmp_path / "requests.db", max_rows=100)
    body = "the quick brown fox jumps over the lazy dog. " * 500
    store.enqueue(_record("r1", input_text=body, output_text=body))
    store.close()

    with sqlite3.connect(store.db_path) as conn:
        stored = conn.execute(
            "SELECT LENGTH(payload) FROM request_bodies WHERE request_id = 'r1'"
        ).fetchone()[0]
    assert stored < len(body) * 2 / 10


def test_list_view_truncates_a_compressed_body(store: RequestLogStore) -> None:
    store.enqueue(_record("r1", input_text="y" * (LIST_BODY_PREVIEW_CHARS + 500)))
    store.close()

    rows, _ = store.list_requests(limit=1)
    assert len(rows[0]["input_text"]) == LIST_BODY_PREVIEW_CHARS
    assert rows[0]["input_text_truncated"] is True
    # The detail view still returns the whole thing.
    full = store.get_request("r1")
    assert full is not None
    assert len(full["input_text"]) == LIST_BODY_PREVIEW_CHARS + 500
    assert full["input_text_truncated"] is False


def test_list_view_of_a_compressed_row_keeps_its_shape(store: RequestLogStore) -> None:
    """List rows carry thinking_chars, never thinking_text."""
    store.enqueue(_record("r1", thinking_text="private reasoning"))
    store.close()

    rows, _ = store.list_requests(limit=1)
    assert "thinking_text" not in rows[0]


def test_search_finds_text_inside_compressed_bodies(store: RequestLogStore) -> None:
    store.enqueue(_record("hit", input_text="a needle in the haystack"))
    store.enqueue(_record("miss", input_text="nothing of interest"))
    store.close()

    rows, total = store.list_requests(q="needle")
    assert total == 1
    assert rows[0]["id"] == "hit"
    assert store.stats(q="needle")["total"] == 1


def test_search_is_case_insensitive_like_the_inline_form(
    store: RequestLogStore,
) -> None:
    store.enqueue(_record("r1", input_text="A Needle In The Haystack"))
    store.close()
    assert store.list_requests(q="needle")[1] == 1


def test_search_spans_legacy_inline_rows_and_compressed_rows(tmp_path) -> None:
    """Both storage forms coexist after an upgrade; search must cover both."""
    path = tmp_path / "requests.db"
    legacy = RequestLogStore(path, max_rows=100, compress_bodies=False)
    legacy.enqueue(_record("old", input_text="shared marker, stored inline"))
    legacy.close()

    modern = RequestLogStore(path, max_rows=100)
    modern.enqueue(_record("new", input_text="shared marker, compressed"))
    modern.close()

    rows, total = modern.list_requests(q="shared marker")
    assert total == 2
    assert {row["id"] for row in rows} == {"old", "new"}


def test_rows_written_before_the_upgrade_are_still_readable(tmp_path) -> None:
    path = tmp_path / "requests.db"
    legacy = RequestLogStore(path, max_rows=100, compress_bodies=False)
    legacy.enqueue(_record("old", input_text="written the old way"))
    legacy.close()

    modern = RequestLogStore(path, max_rows=100)
    modern.close()

    row = modern.get_request("old")
    assert row is not None
    assert row["input_text"] == "written the old way"


def test_compression_can_be_turned_off(tmp_path) -> None:
    store = RequestLogStore(
        tmp_path / "requests.db", max_rows=100, compress_bodies=False
    )
    store.enqueue(_record("r1", input_text="kept inline"))
    store.close()

    with sqlite3.connect(store.db_path) as conn:
        inline = conn.execute(
            "SELECT input_text FROM requests WHERE id = 'r1'"
        ).fetchone()[0]
        blobs = conn.execute("SELECT COUNT(*) FROM request_bodies").fetchone()[0]
    assert inline == "kept inline"
    assert blobs == 0
    row = store.get_request("r1")
    assert row is not None
    assert row["input_text"] == "kept inline"


def test_pruning_removes_the_bodies_of_deleted_rows(tmp_path) -> None:
    """Orphaned blobs would defeat the entire point of retention."""
    store = RequestLogStore(tmp_path / "requests.db", max_rows=2)
    base = time.time()
    for index in range(6):
        store.enqueue(_record(f"r{index}", ts_epoch=base + index, input_text="body"))
    store.close()
    store.prune()

    with sqlite3.connect(store.db_path) as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM request_bodies").fetchone()[0]
        orphans = conn.execute(
            "SELECT COUNT(*) FROM request_bodies b"
            " WHERE NOT EXISTS (SELECT 1 FROM requests r WHERE r.id = b.request_id)"
        ).fetchone()[0]
    assert remaining == 2
    assert orphans == 0


def test_clear_removes_bodies_too(store: RequestLogStore) -> None:
    store.enqueue(_record("r1", input_text="body"))
    store.close()
    store.clear()
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM request_bodies").fetchone()[0] == 0


def test_a_corrupt_blob_degrades_instead_of_raising(store: RequestLogStore) -> None:
    store.enqueue(_record("r1", input_text="original"))
    store.close()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE request_bodies SET payload = ? WHERE request_id = 'r1'",
            (b"not zstd at all",),
        )

    row = store.get_request("r1")
    assert row is not None
    assert row["id"] == "r1"
    assert row["input_text"] is None


def test_a_record_with_no_bodies_writes_no_blob(store: RequestLogStore) -> None:
    store.enqueue(
        _record(
            "r1", input_text=None, output_text=None, thinking_text=None, tool_calls=None
        )
    )
    store.close()
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM request_bodies").fetchone()[0] == 0


def _chatty(index: int) -> str:
    """A body shaped like real traffic: a long shared prefix, a small tail."""
    return (
        "You are Claude Code, an AI assistant. Follow the project conventions. " * 40
        + f"\n\nUser turn {index}: please explain the failure in module {index}."
    )


def test_a_dictionary_is_trained_once_there_is_enough_traffic(tmp_path) -> None:
    """A fresh install must start compressing well without waiting for a restart."""
    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=5000)
    for index in range(300):
        store.enqueue(_record(f"r{index}", input_text=_chatty(index)))
    store.close()
    store.enqueue(_record("after", input_text=_chatty(999)))

    trained = RequestLogStore(path, max_rows=5000)
    trained.enqueue(_record("after", input_text=_chatty(999)))
    trained.close()

    with sqlite3.connect(path) as conn:
        dicts = conn.execute("SELECT COUNT(*) FROM body_dictionaries").fetchone()[0]
        used = conn.execute(
            "SELECT dict_id FROM request_bodies WHERE request_id = 'after'"
        ).fetchone()[0]
        # r0 predates training, so it carries no dictionary.
        before = conn.execute(
            "SELECT LENGTH(payload) FROM request_bodies WHERE request_id = 'r0'"
        ).fetchone()[0]
        after = conn.execute(
            "SELECT LENGTH(payload) FROM request_bodies WHERE request_id = 'after'"
        ).fetchone()[0]
    assert dicts == 1
    assert used is not None
    assert after < before


def test_rows_written_before_training_stay_readable_after_it(tmp_path) -> None:
    """Blobs record their own dictionary, so training must never orphan them."""
    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=5000)
    for index in range(300):
        store.enqueue(_record(f"r{index}", input_text=_chatty(index)))
    store.close()

    trained = RequestLogStore(path, max_rows=5000)
    trained.enqueue(_record("after", input_text=_chatty(999)))
    trained.close()

    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT dict_id FROM request_bodies WHERE request_id = 'r0'"
            ).fetchone()[0]
            is None
        )

    old_row = trained.get_request("r0")
    new_row = trained.get_request("after")
    assert old_row is not None and new_row is not None
    assert old_row["input_text"] == _chatty(0)
    assert new_row["input_text"] == _chatty(999)
    # And search still spans both dictionary generations.
    assert trained.list_requests(q="failure in module 999")[1] == 1
    assert trained.list_requests(q="failure in module 0")[1] == 1


def test_training_does_not_repeat_on_every_restart(tmp_path) -> None:
    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=5000)
    for index in range(300):
        store.enqueue(_record(f"r{index}", input_text=_chatty(index)))
    store.close()

    for _ in range(3):
        reopened = RequestLogStore(path, max_rows=5000)
        reopened.close()

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM body_dictionaries").fetchone()[0] == 1


def test_close_drains_a_deep_queue_instead_of_abandoning_it(tmp_path) -> None:
    """Regression: a fixed close deadline silently dropped queued records.

    Compressing bodies is real CPU work on the writer thread, so a backlog can
    outlive a fixed timeout. Replaying 4,000 real requests lost 2,950 of them
    before the shutdown wait was made to scale with the queue.
    """
    store = RequestLogStore(tmp_path / "requests.db", max_rows=100_000)
    body = "a plausible assistant reply with some structure. " * 500
    for index in range(1_500):
        store.enqueue(_record(f"r{index}", input_text=body, output_text=body))
    store.close()

    _, total = store.list_requests(limit=1)
    assert total == 1_500
    with sqlite3.connect(store.db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM request_bodies").fetchone()[0] == 1_500
        )


def test_search_still_matches_needles_that_json_escapes(store: RequestLogStore) -> None:
    """The byte-level prefilter is skipped for these, not silently wrong.

    Quotes, backslashes and newlines are rewritten by JSON encoding, so a
    needle containing one does not survive into the stored blob byte for byte.
    Such needles must fall back to decoding rather than report no match.
    """
    store.enqueue(_record("quoted", input_text='he said "deploy now" firmly'))
    store.enqueue(_record("slashed", input_text=r"path is C:\Users\fgghk"))
    store.enqueue(_record("newline", input_text="first line\nsecond line"))
    store.close()

    assert store.list_requests(q='"deploy now"')[1] == 1
    assert store.list_requests(q=r"C:\Users")[1] == 1
    assert store.list_requests(q="line\nsecond")[1] == 1


def test_search_does_not_match_the_payload_structure(store: RequestLogStore) -> None:
    """A hit on the encoding must be verified against the real text."""
    store.enqueue(_record("r1", input_text="hello", output_text="world"))
    store.close()

    # These appear in the stored JSON but in no body.
    for structural in ('","', '{"i":', '"o"'):
        assert store.list_requests(q=structural)[1] == 0, structural


def test_search_matches_the_same_rows_with_and_without_compression(tmp_path) -> None:
    """Compression must not change which requests a search finds."""
    texts = [
        "deploy the kubernetes cluster",
        "KUBERNETES in shouty caps",
        "nothing relevant here",
        'a "quoted" phrase',
    ]
    results = {}
    for label, compress in (("inline", False), ("compressed", True)):
        store = RequestLogStore(
            tmp_path / f"{label}.db", max_rows=1000, compress_bodies=compress
        )
        for index, text in enumerate(texts):
            store.enqueue(_record(f"r{index}", input_text=text, output_text=""))
        store.close()
        results[label] = {
            term: {row["id"] for row in store.list_requests(q=term)[0]}
            for term in ("kubernetes", "KUBERNETES", '"quoted"', "relevant", "zzz")
        }
    assert results["inline"] == results["compressed"]
