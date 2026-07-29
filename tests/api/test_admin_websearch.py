"""Admin websearch analytics endpoints: guard, stats shape, filters, pagination."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from free_claude_code.api.admin_websearch_routes import get_websearch_log_store
from free_claude_code.websearch.analytics import WebSearchLogStore
from free_claude_code.websearch.registry import SearchOutcome
from tests.api.support import create_test_app


def _ts(iso_text: str) -> float:
    return datetime.fromisoformat(iso_text).timestamp()


_T1 = _ts("2026-06-01T10:00:00+00:00")
_T2 = _ts("2026-06-08T10:00:00+00:00")
_T3 = _ts("2026-06-15T10:00:00+00:00")
_T4 = _ts("2026-06-22T10:00:00+00:00")


def _outcome(
    *,
    ts_epoch: float,
    provider: str,
    query: str,
    key_label: str = "exak…1234",
    results_count: int,
    status: str = "success",
    error_kind: str | None = None,
    error_message: str | None = None,
    cost_usd: float | None = None,
) -> SearchOutcome:
    return SearchOutcome(
        ts_epoch=ts_epoch,
        ts_iso=datetime.fromtimestamp(ts_epoch, tz=UTC).isoformat(),
        provider=provider,
        key_index=0,
        key_label=key_label,
        query=query,
        results_count=results_count,
        duration_ms=12.5,
        status=status,
        error_kind=error_kind,
        error_message=error_message,
        cost_usd=cost_usd,
        input_payload={"query": query, "max_results": 10},
        output_payload={
            "provider": provider,
            "answer": f"Summary for {query}",
            "results": [
                {
                    "title": query.title(),
                    "url": f"https://example.test/{query.replace(' ', '-')}",
                    "snippet": f"Snippet for {query}",
                    "content": f"Full content for {query}",
                    "published": "2026-06-01",
                }
            ]
            if results_count
            else [],
            "result_count": results_count,
        },
        provider_config={
            "provider_id": provider,
            "credential_count": 1 if key_label else 0,
            "options": {"TEST_MODE": "rich"},
        },
    )


def _seed(store: WebSearchLogStore) -> None:
    store.record(
        _outcome(ts_epoch=_T1, provider="exa", query="apple pie", results_count=5)
    )
    store.record(
        _outcome(
            ts_epoch=_T2,
            provider="exa",
            query="banana bread",
            results_count=0,
            status="error",
            error_kind="rate_limit",
            error_message="429 too many",
        )
    )
    store.record(
        _outcome(
            ts_epoch=_T3,
            provider="tavily",
            query="apple",
            key_label="tvly…zzzz",
            results_count=2,
            cost_usd=0.01,
        )
    )
    store.record(
        _outcome(
            ts_epoch=_T4, provider="ddgs", query="cherry", key_label="", results_count=4
        )
    )
    store.flush()


@pytest.fixture
def log_store(tmp_path):
    store = WebSearchLogStore(tmp_path / "logs" / "websearch.db")
    yield store
    store.close()


@pytest.fixture
def client(log_store, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    app = create_test_app()
    app.dependency_overrides[get_websearch_log_store] = lambda: log_store
    yield TestClient(app, client=("127.0.0.1", 50000))
    app.dependency_overrides.clear()


class TestLoopbackGuard:
    @pytest.mark.parametrize(
        ("method", "path"),
        (
            ("get", "/admin/api/websearch/stats"),
            ("get", "/admin/api/websearch/requests"),
            ("get", "/admin/api/websearch/requests/1"),
            ("delete", "/admin/api/websearch/requests"),
        ),
    )
    def test_remote_clients_are_forbidden(self, client, method, path) -> None:
        remote = TestClient(client.app, client=("203.0.113.10", 50000))

        response = getattr(remote, method)(path)

        assert response.status_code == 403

    def test_non_local_origin_is_forbidden(self, client) -> None:
        response = client.get(
            "/admin/api/websearch/stats",
            headers={"origin": "https://evil.example"},
        )

        assert response.status_code == 403


class TestStatsEndpoint:
    def test_stats_defaults_to_weekly_with_full_shape(self, client, log_store) -> None:
        _seed(log_store)

        response = client.get("/admin/api/websearch/stats")

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["period"] == "weekly"
        assert body["totals"] == {
            "requests": 4,
            "successes": 3,
            "errors": 1,
            "avg_duration_ms": 12.5,
            "results": 11,
            "cost_usd": 0.01,
        }
        providers = {entry["provider"]: entry for entry in body["by_provider"]}
        assert providers["exa"]["requests"] == 2
        assert providers["exa"]["errors"] == 1
        assert providers["tavily"]["avg_duration_ms"] == 12.5
        keys = {(entry["provider"], entry["key_label"]) for entry in body["by_key"]}
        assert keys == {
            ("exa", "exak…1234"),
            ("tavily", "tvly…zzzz"),
            ("ddgs", ""),
        }
        assert body["series"] == [
            {
                "bucket": "2026-W23",
                "provider": "exa",
                "requests": 1,
                "errors": 0,
                "results": 5,
            },
            {
                "bucket": "2026-W24",
                "provider": "exa",
                "requests": 1,
                "errors": 1,
                "results": 0,
            },
            {
                "bucket": "2026-W25",
                "provider": "tavily",
                "requests": 1,
                "errors": 0,
                "results": 2,
            },
            {
                "bucket": "2026-W26",
                "provider": "ddgs",
                "requests": 1,
                "errors": 0,
                "results": 4,
            },
        ]
        assert body["top_errors"] == [
            {"error_kind": "rate_limit", "error_message": "429 too many", "count": 1}
        ]

    @pytest.mark.parametrize("period", ("hourly", "daily", "weekly", "monthly"))
    def test_stats_accepts_supported_periods(self, client, log_store, period) -> None:
        _seed(log_store)

        response = client.get("/admin/api/websearch/stats", params={"period": period})

        assert response.status_code == 200
        body = response.json()
        assert body["period"] == period

    def test_stats_monthly_period(self, client, log_store) -> None:
        _seed(log_store)

        body = client.get(
            "/admin/api/websearch/stats", params={"period": "monthly"}
        ).json()

        assert body["series"] == [
            {
                "bucket": "2026-06",
                "provider": "ddgs",
                "requests": 1,
                "errors": 0,
                "results": 4,
            },
            {
                "bucket": "2026-06",
                "provider": "exa",
                "requests": 2,
                "errors": 1,
                "results": 5,
            },
            {
                "bucket": "2026-06",
                "provider": "tavily",
                "requests": 1,
                "errors": 0,
                "results": 2,
            },
        ]

    def test_stats_filters_apply_to_every_response_section(
        self, client, log_store
    ) -> None:
        _seed(log_store)

        response = client.get(
            "/admin/api/websearch/stats",
            params={
                "period": "daily",
                "provider": "exa",
                "status": "error",
                "q": "banana",
                "since": "2026-06-08T00:00:00+00:00",
                "until": "2026-06-08T23:59:59+00:00",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["filters"] == {
            "provider": "exa",
            "status": "error",
            "q": "banana",
            "since_epoch": _ts("2026-06-08T00:00:00+00:00"),
            "until_epoch": _ts("2026-06-08T23:59:59+00:00"),
        }
        assert body["window"] == {
            "since_epoch": _ts("2026-06-08T00:00:00+00:00"),
            "until_epoch": _ts("2026-06-08T23:59:59+00:00"),
        }
        assert body["dropped_records"] == 0
        assert body["totals"]["requests"] == 1
        assert body["totals"]["errors"] == 1
        assert [entry["provider"] for entry in body["by_provider"]] == ["exa"]
        assert len(body["by_key"]) == 1
        assert body["top_errors"] == [
            {
                "error_kind": "rate_limit",
                "error_message": "429 too many",
                "count": 1,
            }
        ]
        assert body["series"] == [
            {
                "bucket": "2026-06-08",
                "provider": "exa",
                "requests": 1,
                "errors": 1,
                "results": 0,
            }
        ]

    def test_stats_invalid_period_is_422(self, client) -> None:
        response = client.get("/admin/api/websearch/stats", params={"period": "yearly"})

        assert response.status_code == 422

    def test_stats_invalid_since_is_400(self, client) -> None:
        response = client.get(
            "/admin/api/websearch/stats", params={"since": "not-a-date"}
        )

        assert response.status_code == 400

    def test_stats_invalid_status_is_422(self, client) -> None:
        response = client.get("/admin/api/websearch/stats", params={"status": "weird"})

        assert response.status_code == 422

    def test_stats_on_empty_store(self, client) -> None:
        body = client.get("/admin/api/websearch/stats").json()

        assert body["totals"]["requests"] == 0
        assert body["by_provider"] == []
        assert body["series"] == []


class TestRequestsEndpoint:
    def test_pagination_returns_newest_first_with_total(
        self, client, log_store
    ) -> None:
        _seed(log_store)

        body = client.get(
            "/admin/api/websearch/requests", params={"limit": 2, "offset": 1}
        ).json()

        assert body["total"] == 4
        assert body["limit"] == 2
        assert body["offset"] == 1
        assert [item["query"] for item in body["items"]] == ["apple", "banana bread"]
        item = body["items"][0]
        assert item["provider"] == "tavily"
        assert item["key_label"] == "tvly…zzzz"
        assert item["ts_iso"] == "2026-06-15T10:00:00+00:00"
        assert item["status"] == "success"
        assert item["content_captured"] is True
        assert "input" not in item
        assert "output" not in item
        assert "provider_config" not in item

    def test_detail_returns_captured_io_and_provider_config(
        self, client, log_store
    ) -> None:
        _seed(log_store)
        summary = client.get(
            "/admin/api/websearch/requests",
            params={"provider": "tavily"},
        ).json()["items"][0]

        response = client.get(f"/admin/api/websearch/requests/{summary['id']}")

        assert response.status_code == 200
        detail = response.json()
        assert detail["input"] == {"max_results": 10, "query": "apple"}
        assert detail["output"]["answer"] == "Summary for apple"
        assert detail["output"]["results"][0]["content"] == "Full content for apple"
        assert detail["provider_config"] == {
            "credential_count": 1,
            "options": {"TEST_MODE": "rich"},
            "provider_id": "tavily",
        }
        assert len(detail["input_sha256"]) == 64
        assert len(detail["output_sha256"]) == 64

    def test_include_content_supports_complete_json_export(
        self, client, log_store
    ) -> None:
        _seed(log_store)

        body = client.get(
            "/admin/api/websearch/requests",
            params={"provider": "exa", "include_content": "true"},
        ).json()

        assert body["total"] == 2
        assert all("input" in item for item in body["items"])
        assert all("output" in item for item in body["items"])
        assert all("provider_config" in item for item in body["items"])

    def test_detail_unknown_id_is_404(self, client) -> None:
        response = client.get("/admin/api/websearch/requests/999999")

        assert response.status_code == 404
        assert response.json()["detail"] == "web search request not found"

    def test_provider_and_status_filters(self, client, log_store) -> None:
        _seed(log_store)

        by_provider = client.get(
            "/admin/api/websearch/requests", params={"provider": "exa"}
        ).json()
        assert by_provider["total"] == 2

        by_status = client.get(
            "/admin/api/websearch/requests", params={"status": "error"}
        ).json()
        assert by_status["total"] == 1
        assert by_status["items"][0]["query"] == "banana bread"
        assert by_status["items"][0]["error_kind"] == "rate_limit"

    def test_q_filter_matches_query_substring(self, client, log_store) -> None:
        _seed(log_store)

        body = client.get("/admin/api/websearch/requests", params={"q": "apple"}).json()

        assert body["total"] == 2
        assert [item["query"] for item in body["items"]] == ["apple", "apple pie"]

    def test_q_filter_matches_captured_output(self, client, log_store) -> None:
        _seed(log_store)

        body = client.get(
            "/admin/api/websearch/requests",
            params={"q": "summary for banana"},
        ).json()

        assert body["total"] == 1
        assert body["items"][0]["query"] == "banana bread"

    def test_since_until_filters_bound_the_window(self, client, log_store) -> None:
        _seed(log_store)

        body = client.get(
            "/admin/api/websearch/requests",
            params={
                "since": "2026-06-08T00:00:00+00:00",
                "until": "2026-06-15T23:59:59+00:00",
            },
        ).json()

        assert body["total"] == 2
        assert [item["query"] for item in body["items"]] == ["apple", "banana bread"]

    def test_naive_date_bounds_are_treated_as_utc(self, client, log_store) -> None:
        _seed(log_store)

        body = client.get(
            "/admin/api/websearch/requests", params={"since": "2026-06-15"}
        ).json()

        assert body["total"] == 2
        assert [item["query"] for item in body["items"]] == ["cherry", "apple"]

    def test_invalid_since_is_400(self, client) -> None:
        response = client.get(
            "/admin/api/websearch/requests", params={"since": "not-a-date"}
        )

        assert response.status_code == 400

    def test_invalid_status_is_422(self, client) -> None:
        response = client.get(
            "/admin/api/websearch/requests", params={"status": "weird"}
        )

        assert response.status_code == 422

    def test_limit_must_be_positive(self, client) -> None:
        response = client.get("/admin/api/websearch/requests", params={"limit": 0})

        assert response.status_code == 422


class TestDeleteEndpoint:
    def test_delete_clears_all_requests(self, client, log_store) -> None:
        _seed(log_store)

        response = client.delete("/admin/api/websearch/requests")

        assert response.status_code == 200
        assert response.json() == {"cleared": True, "deleted": 4}
        assert client.get("/admin/api/websearch/requests").json()["total"] == 0
        stats = client.get("/admin/api/websearch/stats").json()
        assert stats["totals"]["requests"] == 0
