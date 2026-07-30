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


def test_auto_vacuum_is_incremental(tmp_path) -> None:
    store = RequestLogStore(tmp_path / "requests.db", max_rows=10)
    try:
        with sqlite3.connect(store.db_path) as conn:
            mode = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
    finally:
        store.close()
    assert int(mode) == 2


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
