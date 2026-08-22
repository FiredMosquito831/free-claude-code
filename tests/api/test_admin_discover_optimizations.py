"""Admin API tests for the on-demand optimization-rule discovery scan."""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from my_claude_code.core.optimization_discovery import (
    MAX_SCAN_ROW_LIMIT,
    family_signature,
)
from my_claude_code.core.request_log import RequestRecord, get_request_log_store
from tests.api.support import create_test_app

ENDPOINT = "/admin/api/requests/discover-optimizations"
HEADER_LINE = "x-anthropic-billing-header: cc_version=2.1.235.2db; cc_entrypoint=cli;"
KEBAB_PROMPT = "Generate a short kebab-case name (2-4 words) that describes this.\nRespond with only the name."
TITLE_PROMPT = "Please write a 5-10 word title for the following conversation.\nRespond with the title only."


@pytest.fixture
def client():
    return TestClient(create_test_app(), client=("127.0.0.1", 50000))


@pytest.fixture
def remote_client():
    return TestClient(create_test_app(), client=("10.0.0.5", 50000))


def _record(index: int, prompt: str, *, base: float, optimization=None):
    return RequestRecord(
        id=f"r{index:04d}",
        endpoint="/v1/messages",
        protocol="anthropic",
        ts_epoch=base + index,
        provider=None if optimization else "p1",
        resolved_model="m1",
        input_text=f"{HEADER_LINE}\n{prompt}",
        output_text="answer",
        input_chars=len(prompt),
        output_chars=6,
        tokens_in=100,
        tokens_out=10,
        params={"tools_count": 0},
        optimization=optimization,
    )


@pytest.fixture
def seeded_store(tmp_path):
    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    base = 1_700_000_000.0
    for index in range(6):
        store.enqueue(_record(index, KEBAB_PROMPT, base=base))
    for index in range(3):
        store.enqueue(
            _record(
                100 + index,
                TITLE_PROMPT,
                base=base,
                optimization="title_generation_skip",
            )
        )
    store.enqueue(_record(200, "a one-off prompt nobody repeats", base=base))
    store.close()
    yield store


@pytest.fixture
def empty_store(tmp_path):
    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    store.close()
    yield store


def test_scan_is_local_only(remote_client) -> None:
    response = remote_client.get(ENDPOINT)

    assert response.status_code == 403
    assert response.json()["detail"] == "Admin UI is local-only"


def test_empty_database_returns_a_sane_body(client, empty_store) -> None:
    response = client.get(ENDPOINT)

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is True
    assert payload["candidates"] == []
    assert payload["covered"] == []
    assert payload["scanned"]["rows"] == 0
    assert payload["scanned"]["truncated"] is False
    assert payload["scanned"]["row_limit"] > 0


def test_scan_reports_candidates_and_excludes_what_a_rule_already_answers(
    client, seeded_store
) -> None:
    payload = client.get(ENDPOINT).json()

    assert payload["scanned"]["rows"] == 10
    assert payload["scanned"]["matching_rows"] == 10
    assert payload["scanned"]["truncated"] is False
    assert [family["signature"] for family in payload["candidates"]] == [
        family_signature(KEBAB_PROMPT)
    ]
    assert [family["signature"] for family in payload["covered"]] == [
        family_signature(TITLE_PROMPT)
    ]
    candidate = payload["candidates"][0]
    assert candidate["requests"] == 6
    assert candidate["tokens_total"] == 6 * 110
    assert candidate["sample_request_id"] == "r0005"
    assert candidate["optimized_requests"] == 0


def test_row_limit_bound_is_honoured_and_stated(client, seeded_store) -> None:
    payload = client.get(ENDPOINT, params={"row_limit": 4}).json()

    assert payload["scanned"]["rows"] == 4
    assert payload["scanned"]["row_limit"] == 4
    assert payload["scanned"]["matching_rows"] == 10
    assert payload["scanned"]["truncated"] is True


def test_time_window_bound_is_honoured_and_stated(client, seeded_store) -> None:
    payload = client.get(
        ENDPOINT, params={"since": 1_700_000_100.0, "until": 1_700_000_150.0}
    ).json()

    assert payload["scanned"]["rows"] == 3
    assert payload["scanned"]["since"] == 1_700_000_100.0
    assert payload["scanned"]["until"] == 1_700_000_150.0
    assert payload["candidates"] == []
    assert payload["covered"][0]["requests"] == 3


def test_row_limit_above_the_ceiling_is_refused_rather_than_silently_clamped(
    client, seeded_store
) -> None:
    """A silently clamped bound would make a sample look like a full scan."""
    response = client.get(ENDPOINT, params={"row_limit": MAX_SCAN_ROW_LIMIT + 1})

    assert response.status_code == 422
    assert str(MAX_SCAN_ROW_LIMIT) in response.json()["detail"]


def test_scan_reports_disabled_when_the_request_log_is_off(monkeypatch) -> None:
    from my_claude_code.api import admin_routes

    monkeypatch.setattr(
        admin_routes, "_request_log_store_or_none", lambda _settings: None
    )
    client = TestClient(create_test_app(), client=("127.0.0.1", 50000))

    payload = client.get(ENDPOINT).json()

    assert payload == {"enabled": False}


def test_scan_does_not_block_the_event_loop(client, seeded_store, monkeypatch) -> None:
    """The scan is seconds of blocking CPU; it must run off the event loop.

    Asserted by the one signal that distinguishes the two without timing: a
    running event loop is visible from the coroutine's own thread and from
    nowhere else, so ``get_running_loop`` succeeding inside the clustering
    call means the clustering is running on the loop. Checking the thread
    identity instead is not enough -- ``TestClient`` drives the loop on a
    worker thread of its own, so "not the main thread" is true either way,
    and that version of this test let the mutation survive.
    """
    from my_claude_code.api import admin_routes

    real = admin_routes.discover_families
    observed: list[bool] = []

    def guarded(*args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            observed.append(True)
        else:
            observed.append(False)
        return real(*args, **kwargs)

    monkeypatch.setattr(admin_routes, "discover_families", guarded)

    assert client.get(ENDPOINT).status_code == 200
    assert observed == [True]


def test_repeated_scans_do_not_change_the_log(client, seeded_store) -> None:
    """Discovery observes; it never causes a request to be answered locally."""
    before = client.get("/admin/api/requests", params={"limit": 500}).json()
    client.get(ENDPOINT)
    client.get(ENDPOINT)
    after = client.get("/admin/api/requests", params={"limit": 500}).json()

    assert before["total"] == after["total"]
    assert [row["optimization"] for row in before["rows"]] == [
        row["optimization"] for row in after["rows"]
    ]


def test_elapsed_time_is_reported_so_a_slow_scan_is_visible(
    client, seeded_store
) -> None:
    started = time.perf_counter()
    payload = client.get(ENDPOINT).json()
    wall_ms = (time.perf_counter() - started) * 1000.0

    assert payload["scanned"]["elapsed_ms"] >= 0.0
    assert payload["scanned"]["elapsed_ms"] <= wall_ms
