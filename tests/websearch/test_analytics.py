"""WebSearchLogStore round-trips, rollups, retention, and the shared singleton."""

import sqlite3
import threading
from datetime import UTC, datetime

import pytest

from free_claude_code.websearch.analytics import (
    WebSearchLogStore,
    default_websearch_db_path,
    get_shared_store,
    record_search,
    reset_analytics_state,
)
from free_claude_code.websearch.registry import SearchOutcome, search
from tests.websearch.support import StubWebSearchProvider, build_config

_BASE_TS = datetime(2026, 6, 15, 12, 0, tzinfo=UTC).timestamp()


def _ts(iso_text: str) -> float:
    return datetime.fromisoformat(iso_text).timestamp()


def _outcome(
    *,
    ts_epoch: float = _BASE_TS,
    provider: str = "exa",
    key_index: int = 0,
    key_label: str = "exak…1234",
    query: str = "query",
    results_count: int = 3,
    duration_ms: float = 12.5,
    status: str = "success",
    error_kind: str | None = None,
    error_message: str | None = None,
    cost_usd: float | None = None,
) -> SearchOutcome:
    return SearchOutcome(
        ts_epoch=ts_epoch,
        ts_iso=datetime.fromtimestamp(ts_epoch, tz=UTC).isoformat(),
        provider=provider,
        key_index=key_index,
        key_label=key_label,
        query=query,
        results_count=results_count,
        duration_ms=duration_ms,
        status=status,
        error_kind=error_kind,
        error_message=error_message,
        cost_usd=cost_usd,
    )


@pytest.fixture(autouse=True)
def _isolated_analytics_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("WEBSEARCH_LOG_ENABLED", raising=False)
    monkeypatch.delenv("WEBSEARCH_LOG_MAX_ROWS", raising=False)
    reset_analytics_state()
    yield
    reset_analytics_state()


@pytest.fixture
def store(tmp_path):
    store = WebSearchLogStore(tmp_path / "logs" / "websearch.db")
    yield store
    store.close()


class TestRoundTrip:
    def test_recorded_outcomes_round_trip_all_fields(self, store) -> None:
        ts = _ts("2026-06-15T08:30:00+00:00")
        store.record(
            _outcome(
                ts_epoch=ts,
                provider="exa",
                key_index=1,
                key_label="exak…wxyz",
                query="best tacos",
                results_count=7,
                duration_ms=42.25,
                cost_usd=0.003,
            )
        )
        store.record(
            _outcome(
                ts_epoch=ts + 1,
                provider="tavily",
                query="failed query",
                results_count=0,
                duration_ms=5.0,
                status="error",
                error_kind="rate_limit",
                error_message="429 slow down",
            )
        )
        store.flush()

        page = store.requests()
        assert page["total"] == 2
        newest, oldest = page["items"]
        assert newest["provider"] == "tavily"
        assert newest["status"] == "error"
        assert newest["error_kind"] == "rate_limit"
        assert newest["error_message"] == "429 slow down"
        assert newest["results_count"] == 0
        assert newest["cost_usd"] is None
        assert oldest["provider"] == "exa"
        assert oldest["key_index"] == 1
        assert oldest["key_label"] == "exak…wxyz"
        assert oldest["query"] == "best tacos"
        assert oldest["results_count"] == 7
        assert oldest["duration_ms"] == 42.25
        assert oldest["cost_usd"] == 0.003
        assert oldest["ts_epoch"] == ts
        assert oldest["ts_iso"] == "2026-06-15T08:30:00+00:00"
        assert oldest["status"] == "success"
        assert oldest["error_kind"] is None
        assert oldest["error_message"] is None
        assert oldest["id"] > 0

    def test_query_and_error_message_are_capped(self, store) -> None:
        store.record(
            _outcome(
                query="x" * 1000,
                status="error",
                error_kind="upstream",
                error_message="e" * 1000,
                results_count=0,
            )
        )
        store.flush()

        (row,) = store.requests()["items"]
        assert len(row["query"]) == 256
        assert len(row["error_message"]) == 500

    @pytest.mark.asyncio
    async def test_recorded_label_is_the_masked_key(self, store) -> None:
        provider = StubWebSearchProvider(build_config(api_keys=("sk-live-0001wxyz",)))

        response = await search(provider, "hello", recorder=store.record)

        assert response.results
        store.flush()
        (row,) = store.requests()["items"]
        assert row["key_label"] == "sk-l…wxyz"
        assert "sk-live-0001wxyz" not in row["key_label"]
        assert row["status"] == "success"
        assert row["results_count"] == 1


class TestRetention:
    def test_prunes_to_max_rows_keeping_newest(self, tmp_path) -> None:
        store = WebSearchLogStore(tmp_path / "websearch.db", max_rows=5, prune_every=1)
        for index in range(7):
            store.record(_outcome(query=f"q{index}", ts_epoch=_BASE_TS + index))
        store.flush()

        items = store.requests(limit=50)["items"]
        assert [row["query"] for row in items] == ["q6", "q5", "q4", "q3", "q2"]
        store.close()


def _record_boundary_rows(store: WebSearchLogStore) -> None:
    # 2025-12-28 (Sun) is ISO week 2025-W52; 2025-12-29 (Mon) is ISO week 2026-W01.
    store.record(_outcome(ts_epoch=_ts("2025-12-28T12:00:00+00:00"), provider="exa"))
    store.record(_outcome(ts_epoch=_ts("2025-12-29T12:00:00+00:00"), provider="exa"))
    store.record(
        _outcome(
            ts_epoch=_ts("2026-01-01T00:30:00+00:00"),
            provider="tavily",
            results_count=0,
            status="error",
            error_kind="quota",
            error_message="quota exceeded",
        )
    )
    store.flush()


class TestStats:
    def test_weekly_series_uses_iso_weeks_across_year_boundary(self, store) -> None:
        _record_boundary_rows(store)

        stats = store.stats("weekly")

        assert stats["period"] == "weekly"
        assert stats["series"] == [
            {
                "bucket": "2025-W52",
                "provider": "exa",
                "requests": 1,
                "errors": 0,
                "results": 3,
            },
            {
                "bucket": "2026-W01",
                "provider": "exa",
                "requests": 1,
                "errors": 0,
                "results": 3,
            },
            {
                "bucket": "2026-W01",
                "provider": "tavily",
                "requests": 1,
                "errors": 1,
                "results": 0,
            },
        ]

    def test_monthly_series_buckets_by_month_across_year_boundary(self, store) -> None:
        _record_boundary_rows(store)

        stats = store.stats("monthly")

        assert stats["period"] == "monthly"
        assert stats["series"] == [
            {
                "bucket": "2025-12",
                "provider": "exa",
                "requests": 2,
                "errors": 0,
                "results": 6,
            },
            {
                "bucket": "2026-01",
                "provider": "tavily",
                "requests": 1,
                "errors": 1,
                "results": 0,
            },
        ]

    def test_by_provider_and_totals_aggregation(self, store) -> None:
        store.record(_outcome(provider="exa", duration_ms=10.0, results_count=4))
        store.record(
            _outcome(
                provider="exa",
                duration_ms=20.0,
                results_count=0,
                status="error",
                error_kind="upstream",
                error_message="boom",
            )
        )
        store.record(
            _outcome(
                provider="tavily", duration_ms=30.0, results_count=2, cost_usd=0.01
            )
        )
        store.flush()

        stats = store.stats()

        assert stats["totals"] == {
            "requests": 3,
            "successes": 2,
            "errors": 1,
            "avg_duration_ms": 20.0,
            "results": 6,
            "cost_usd": 0.01,
        }
        exa, tavily = stats["by_provider"]
        assert exa == {
            "provider": "exa",
            "requests": 2,
            "errors": 1,
            "avg_duration_ms": 15.0,
            "results": 4,
            "cost_usd": None,
        }
        assert tavily["provider"] == "tavily"
        assert tavily["cost_usd"] == 0.01
        assert stats["top_errors"] == [
            {"error_kind": "upstream", "error_message": "boom", "count": 1}
        ]

    def test_by_key_groups_provider_and_key_label(self, store) -> None:
        store.record(_outcome(provider="exa", key_index=0, key_label="exak…aaaa"))
        store.record(
            _outcome(
                provider="exa",
                key_index=1,
                key_label="exak…bbbb",
                status="error",
                error_kind="auth",
                error_message="401 denied",
                results_count=0,
            )
        )
        store.record(_outcome(provider="tavily", key_index=0, key_label="exak…aaaa"))
        store.flush()

        stats = store.stats()

        assert stats["by_key"] == [
            {
                "provider": "exa",
                "key_label": "exak…aaaa",
                "requests": 1,
                "errors": 0,
                "avg_duration_ms": 12.5,
                "results": 3,
            },
            {
                "provider": "exa",
                "key_label": "exak…bbbb",
                "requests": 1,
                "errors": 1,
                "avg_duration_ms": 12.5,
                "results": 0,
            },
            {
                "provider": "tavily",
                "key_label": "exak…aaaa",
                "requests": 1,
                "errors": 0,
                "avg_duration_ms": 12.5,
                "results": 3,
            },
        ]

    def test_stats_on_empty_database(self, store) -> None:
        stats = store.stats()

        assert stats == {
            "period": "weekly",
            "totals": {
                "requests": 0,
                "successes": 0,
                "errors": 0,
                "avg_duration_ms": None,
                "results": 0,
                "cost_usd": None,
            },
            "by_provider": [],
            "by_key": [],
            "series": [],
            "top_errors": [],
        }
        assert store.requests() == {"total": 0, "limit": 50, "offset": 0, "items": []}

    def test_unknown_period_rejected(self, store) -> None:
        with pytest.raises(ValueError, match="unknown stats period"):
            store.stats("daily")


class TestRecordSearch:
    def test_disabled_logging_skips_persistence(self, monkeypatch) -> None:
        monkeypatch.setenv("WEBSEARCH_LOG_ENABLED", "false")

        record_search(_outcome())

        assert not default_websearch_db_path().exists()

    def test_enabled_logging_persists_to_default_path(self) -> None:
        record_search(_outcome(query="shared query"))

        store = get_shared_store()
        store.flush()
        page = store.requests()
        assert page["total"] == 1
        assert page["items"][0]["query"] == "shared query"
        assert default_websearch_db_path().is_file()
        assert default_websearch_db_path().parent.name == "logs"
        assert default_websearch_db_path().parent.parent.name == ".fcc"

    def test_shared_store_honors_max_rows_setting(self, monkeypatch) -> None:
        monkeypatch.setenv("WEBSEARCH_LOG_MAX_ROWS", "3")
        store = get_shared_store()

        for index in range(100):
            store.record(_outcome(query=f"bulk {index}", ts_epoch=_BASE_TS + index))
        store.flush()
        # Prune cadence (default 100 inserts) fired: trimmed to the newest 3.
        assert store.requests(limit=500)["total"] == 3

        for index in range(100, 105):
            store.record(_outcome(query=f"bulk {index}", ts_epoch=_BASE_TS + index))
        store.flush()

        page = store.requests(limit=500)
        assert page["total"] == 8
        assert page["items"][0]["query"] == "bulk 104"


class TestWriterBehavior:
    def test_full_queue_drops_new_records(self, monkeypatch, tmp_path) -> None:
        release = threading.Event()
        monkeypatch.setattr(
            WebSearchLogStore,
            "_writer_main",
            lambda self: release.wait(5),
        )
        store = WebSearchLogStore(tmp_path / "websearch.db", queue_cap=1)

        assert store.record(_outcome()) is True
        assert store.record(_outcome()) is False
        assert store.dropped == 1

        release.set()
        store.close()

    def test_close_drains_pending_records(self, tmp_path) -> None:
        db_path = tmp_path / "websearch.db"
        store = WebSearchLogStore(db_path)
        for index in range(3):
            store.record(_outcome(query=f"drain {index}", ts_epoch=_BASE_TS + index))

        assert store.close(timeout=5.0) is True
        assert store.record(_outcome()) is False

        connection = sqlite3.connect(db_path)
        try:
            count = connection.execute("SELECT COUNT(*) FROM search_log").fetchone()[0]
        finally:
            connection.close()
        assert count == 3

    def test_flush_after_close_is_a_noop(self, store) -> None:
        store.close()
        store.flush()
