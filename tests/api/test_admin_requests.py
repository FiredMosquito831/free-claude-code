"""Admin API tests for the request log endpoints."""

import time

import pytest
from fastapi.testclient import TestClient

from free_claude_code.core.request_log import (
    RequestRecord,
    get_request_log_store,
)
from tests.api.support import create_test_app


@pytest.fixture
def client():
    return TestClient(create_test_app(), client=("127.0.0.1", 50000))


@pytest.fixture
def seeded_store(tmp_path):
    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    base = time.time()
    records = [
        RequestRecord(
            id=f"r{index}",
            endpoint="/v1/messages" if index % 2 == 0 else "/v1/responses",
            protocol="anthropic",
            provider="p1" if index % 2 == 0 else "p2",
            resolved_model="m1",
            ts_epoch=base + index,
            status="error" if index == 4 else "success",
            error_message="boom" if index == 4 else None,
            tokens_in=10 * index,
            tokens_out=index,
            duration_ms=float(100 * (index + 1)),
            input_text="in" * 3000,
            output_text="out",
        )
        for index in range(5)
    ]
    for record in records:
        store.enqueue(record)
    store.close()
    yield store


def test_list_requests_paging_and_filters(client, seeded_store) -> None:
    response = client.get("/admin/api/requests", params={"limit": 2, "offset": 0})
    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is True
    assert payload["capture_bodies"] is True
    assert payload["total"] == 5
    assert [row["id"] for row in payload["rows"]] == ["r4", "r3"]

    page = client.get("/admin/api/requests", params={"limit": 2, "offset": 4}).json()
    assert [row["id"] for row in page["rows"]] == ["r0"]

    by_provider = client.get("/admin/api/requests", params={"provider": "p2"}).json()
    assert by_provider["total"] == 2

    by_status = client.get("/admin/api/requests", params={"status": "error"}).json()
    assert by_status["total"] == 1
    assert by_status["rows"][0]["id"] == "r4"

    by_endpoint = client.get(
        "/admin/api/requests", params={"endpoint": "/v1/responses"}
    ).json()
    assert by_endpoint["total"] == 2

    invalid = client.get("/admin/api/requests", params={"status": "nope"})
    assert invalid.status_code == 422


def test_list_truncates_bodies(client, seeded_store) -> None:
    payload = client.get("/admin/api/requests").json()
    row = payload["rows"][0]
    assert len(row["input_text"]) == 4096
    assert row["input_text_truncated"] is True

    full = client.get(f"/admin/api/requests/{row['id']}").json()
    assert len(full["input_text"]) == 6000
    assert full["input_text_truncated"] is False


def test_get_missing_entry_404(client, seeded_store) -> None:
    assert client.get("/admin/api/requests/nope").status_code == 404


def test_stats_endpoint(client, seeded_store) -> None:
    stats = client.get("/admin/api/requests/stats").json()
    assert stats["enabled"] is True
    assert stats["total"] == 5
    assert stats["error"] == 1
    assert stats["error_rate"] == pytest.approx(0.2)
    assert stats["tokens_in"] == 100
    assert stats["tokens_out"] == 10
    assert stats["p50_duration_ms"] == pytest.approx(300.0)
    assert {entry["key"] for entry in stats["by_provider"]} == {"p1", "p2"}
    assert stats["top_errors"] == [{"message": "boom", "count": 1}]
    assert len(stats["series"]) >= 1

    windowed = client.get(
        "/admin/api/requests/stats", params={"since": time.time() + 1000}
    ).json()
    assert windowed["total"] == 0


@pytest.mark.parametrize(
    ("params", "expected_total"),
    [
        ({"provider": "p2"}, 2),
        ({"model": "m1"}, 5),
        ({"status": "error"}, 1),
        ({"endpoint": "/v1/responses"}, 2),
        ({"q": "inin"}, 5),
    ],
)
def test_stats_endpoint_applies_request_filters(
    client, seeded_store, params, expected_total
) -> None:
    stats = client.get("/admin/api/requests/stats", params=params).json()

    assert stats["total"] == expected_total
    assert sum(entry["requests"] for entry in stats["by_provider"]) == expected_total
    assert sum(entry["requests"] for entry in stats["by_model"]) == expected_total
    assert sum(point["requests"] for point in stats["series"]) == expected_total


def test_stats_endpoint_filter_changes_cards_breakdowns_series_and_errors(
    client, seeded_store
) -> None:
    stats = client.get(
        "/admin/api/requests/stats",
        params={
            "provider": "p1",
            "model": "m1",
            "status": "error",
            "endpoint": "/v1/messages",
            "q": "inin",
        },
    ).json()

    assert stats["total"] == 1
    assert stats["success"] == 0
    assert stats["error"] == 1
    assert stats["tokens_in"] == 40
    assert stats["tokens_out"] == 4
    assert stats["p50_duration_ms"] == 500.0
    assert stats["by_provider"] == [
        {
            "key": "p1",
            "requests": 1,
            "tokens_in": 40,
            "tokens_out": 4,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "cache_reported": 0,
            "errors": 1,
            "avg_duration_ms": 500.0,
        }
    ]
    assert stats["by_model"][0]["requests"] == 1
    assert stats["top_errors"] == [{"message": "boom", "count": 1}]
    assert sum(point["requests"] for point in stats["series"]) == 1
    assert sum(point["errors"] for point in stats["series"]) == 1


def test_stats_endpoint_rejects_invalid_status(client, seeded_store) -> None:
    response = client.get(
        "/admin/api/requests/stats", params={"status": "not-a-status"}
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid status filter"


def test_clear_requests(client, seeded_store) -> None:
    response = client.request("DELETE", "/admin/api/requests")
    assert response.status_code == 200
    assert response.json()["cleared"] == 5
    assert client.get("/admin/api/requests").json()["total"] == 0


def test_request_log_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("REQUEST_LOG_ENABLED", "false")
    from free_claude_code.config.settings import Settings
    from tests.api.support import create_test_app as make_app

    app = make_app(Settings())
    disabled_client = TestClient(app, client=("127.0.0.1", 50000))
    payload = disabled_client.get("/admin/api/requests").json()
    assert payload["enabled"] is False
    assert payload["rows"] == []
    stats = disabled_client.get("/admin/api/requests/stats").json()
    assert stats["enabled"] is False
    cleared = disabled_client.request("DELETE", "/admin/api/requests").json()
    assert cleared["cleared"] == 0


def test_admin_requests_loopback_guard(seeded_store) -> None:
    remote = TestClient(create_test_app(), client=("203.0.113.10", 50000))
    assert remote.get("/admin/api/requests").status_code == 403
    assert remote.get("/admin/api/requests/stats").status_code == 403
    assert remote.get("/admin/api/requests/r0").status_code == 403
    assert remote.request("DELETE", "/admin/api/requests").status_code == 403
