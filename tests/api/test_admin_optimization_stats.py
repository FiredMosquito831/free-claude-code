"""The endpoint the Token Optimizer page reads.

Covers the three states a fresh install actually passes through -- logging off,
logging on with nothing recorded, logging on with traffic -- because the page's
whole claim is that it distinguishes them instead of printing zeros over all
three.
"""

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.optimization_handlers import OPTIMIZATION_RULE_SPECS
from my_claude_code.config.settings import Settings
from my_claude_code.core.request_log import RequestRecord, get_request_log_store
from tests.api.support import create_test_app

ENDPOINT = "/admin/api/requests/optimization-stats"
BASE = 1_700_000_000.0


def _record(index: int, *, optimization: str | None, tokens_saved: int | None = None):
    return RequestRecord(
        id=f"r{index:05d}",
        endpoint="/v1/messages",
        protocol="anthropic",
        ts_epoch=BASE + index,
        provider=None if optimization else "p1",
        resolved_model="m1",
        input_text="prompt",
        output_text="answer",
        tokens_in=100,
        tokens_out=10,
        optimization=optimization,
        optimization_tokens_saved=tokens_saved,
    )


@pytest.fixture
def client():
    return TestClient(create_test_app(), client=("127.0.0.1", 50000))


@pytest.fixture
def remote_client():
    return TestClient(create_test_app(), client=("10.0.0.5", 50000))


@pytest.fixture
def empty_store(tmp_path):
    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    store.close()
    yield store


@pytest.fixture
def seeded_store(tmp_path):
    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    for index in range(3):
        store.enqueue(
            _record(index, optimization="title_generation_skip", tokens_saved=500)
        )
    store.enqueue(_record(50, optimization=None))
    store.close()
    yield store


def test_the_endpoint_is_local_only(remote_client) -> None:
    response = remote_client.get(ENDPOINT)

    assert response.status_code == 403
    assert response.json()["detail"] == "Admin UI is local-only"


def test_logging_disabled_still_names_every_rule_and_its_state(client) -> None:
    """A page that cannot measure must still be able to say what is switched on."""
    settings = Settings()
    app_client = TestClient(
        create_test_app(settings.model_copy(update={"request_log_enabled": False})),
        client=("127.0.0.1", 50000),
    )

    payload = app_client.get(ENDPOINT).json()

    assert payload["enabled"] is False
    assert [rule["rule"] for rule in payload["rules"]] == [
        spec.rule for spec in OPTIMIZATION_RULE_SPECS
    ]
    # No measurement is offered, rather than a zero standing in for one.
    assert "tokens_saved" not in payload


def test_an_empty_log_reports_every_rule_with_an_unknown_saving(
    client, empty_store
) -> None:
    payload = client.get(ENDPOINT).json()

    assert payload["enabled"] is True
    assert payload["total_requests"] == 0
    assert payload["answered_locally"] == 0
    for rule in payload["rules"]:
        assert rule["requests"] == 0
        # The count is a real zero. The saving was never measured, so it is
        # null -- an em dash on the page, not "0 tokens saved".
        assert rule["tokens_saved"] is None
        assert rule["daily"] == []


def test_every_rule_carries_the_literal_string_it_answers_with(
    client, empty_store
) -> None:
    rules = {rule["rule"]: rule for rule in client.get(ENDPOINT).json()["rules"]}

    for spec in OPTIMIZATION_RULE_SPECS:
        assert rules[spec.rule]["answer"] == spec.answer
        assert rules[spec.rule]["label"] == spec.label
        assert rules[spec.rule]["env_key"] == spec.env_key


def test_a_rule_reports_the_live_setting_beside_its_number(client, empty_store) -> None:
    disabled = TestClient(
        create_test_app(
            Settings().model_copy(update={"enable_title_generation_skip": False})
        ),
        client=("127.0.0.1", 50000),
    )

    rules = {rule["rule"]: rule for rule in disabled.get(ENDPOINT).json()["rules"]}

    assert rules["title_generation_skip"]["enabled"] is False
    assert rules["suggestion_mode_skip"]["enabled"] is True


def test_measured_rules_report_their_fires_and_savings(client, seeded_store) -> None:
    payload = client.get(ENDPOINT).json()
    rules = {rule["rule"]: rule for rule in payload["rules"]}

    assert payload["total_requests"] == 4
    assert payload["answered_locally"] == 3
    assert payload["tokens_saved"] == 1_500
    assert rules["title_generation_skip"]["requests"] == 3
    assert rules["title_generation_skip"]["tokens_saved"] == 1_500
    # Registered, never fired: still listed, still honest about the saving.
    assert rules["suggestion_mode_skip"]["requests"] == 0
    assert rules["suggestion_mode_skip"]["tokens_saved"] is None


def test_a_rule_no_longer_in_the_registry_is_reported_not_dropped(
    client, tmp_path
) -> None:
    """Retiring a rule must not silently delete the savings it already made."""
    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    store.enqueue(_record(1, optimization="a_retired_rule", tokens_saved=99))
    store.close()

    rules = {rule["rule"]: rule for rule in client.get(ENDPOINT).json()["rules"]}

    assert rules["a_retired_rule"]["requests"] == 1
    assert rules["a_retired_rule"]["tokens_saved"] == 99
    assert rules["a_retired_rule"]["retired"] is True
    assert rules["a_retired_rule"]["enabled"] is None
    # The rule name is all the label we have left, and it is user-visible text.
    assert rules["a_retired_rule"]["label"] == "a_retired_rule"
    assert rules["a_retired_rule"]["answer"] is None
    # The rules that DO still exist are unaffected by a stranger in the log.
    # They carry no `retired` key at all, which is why this reads with .get():
    # absence is the signal, and asserting on rules[...]["retired"] would be
    # asserting that a live rule carries a marker it has no reason to.
    assert rules["title_generation_skip"].get("retired") is not True
    assert rules["title_generation_skip"]["enabled"] is True


def test_the_window_is_passed_through(client, seeded_store) -> None:
    payload = client.get(
        ENDPOINT, params={"since": BASE + 1, "until": BASE + 1.5}
    ).json()

    assert payload["answered_locally"] == 1
    assert payload["window"]["since"] == BASE + 1


def test_a_locally_answered_request_is_not_grouped_under_unknown(
    client, seeded_store
) -> None:
    """The analytics breakdown names the rule, because we know what served it."""
    stats = client.get("/admin/api/requests/stats").json()
    keys = {row["key"] for row in stats["by_provider"]}

    assert "local:title_generation_skip" in keys
    assert "(unknown)" not in keys
