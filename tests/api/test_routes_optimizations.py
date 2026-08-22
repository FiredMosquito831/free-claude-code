from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.dependencies import get_settings
from my_claude_code.api.ports import ApiServices
from my_claude_code.application.ports import StopResult
from my_claude_code.config.settings import Settings
from tests.api.support import create_test_app

app = create_test_app()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def mock_settings():
    settings = Settings()
    settings.enable_title_generation_skip = True
    return settings


def test_create_message_title_generation_skip(client, mock_settings):
    app.dependency_overrides[get_settings] = lambda: mock_settings

    payload = {
        "model": "claude-3-sonnet",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "generate title"}],
    }

    with patch(
        "my_claude_code.api.optimization_handlers.is_title_generation_request",
        return_value=True,
    ):
        response = client.post("/v1/messages", json=payload)

    assert response.status_code == 200
    assert "Conversation" in response.json()["content"][0]["text"]

    app.dependency_overrides.clear()


def test_create_message_empty_messages_returns_400(client):
    """POST /v1/messages with messages: [] returns 400 invalid_request_error."""
    payload = {
        "model": "claude-3-sonnet",
        "max_tokens": 100,
        "messages": [],
    }
    response = client.post("/v1/messages", json=payload)
    assert response.status_code == 400
    data = response.json()
    assert data.get("type") == "error"
    assert data.get("error", {}).get("type") == "invalid_request_error"
    assert "cannot be empty" in data.get("error", {}).get("message", "")


def test_count_tokens_empty_messages_returns_400(client):
    """POST /v1/messages/count_tokens with messages: [] matches messages validation."""
    payload = {"model": "claude-3-sonnet", "messages": []}
    response = client.post("/v1/messages/count_tokens", json=payload)
    assert response.status_code == 400
    data = response.json()
    assert data.get("type") == "error"
    assert data.get("error", {}).get("type") == "invalid_request_error"
    assert "cannot be empty" in data.get("error", {}).get("message", "")


def test_count_tokens_endpoint(client):
    payload = {
        "model": "claude-3-sonnet",
        "messages": [{"role": "user", "content": "hello"}],
    }

    with patch("my_claude_code.api.routes.get_token_count", return_value=5):
        response = client.post("/v1/messages/count_tokens", json=payload)

    assert response.status_code == 200
    assert response.json()["input_tokens"] == 5


def test_count_tokens_error_returns_500(client):
    """When get_token_count raises, count_tokens returns 500."""
    payload = {
        "model": "claude-3-sonnet",
        "messages": [{"role": "user", "content": "hello"}],
    }

    with patch(
        "my_claude_code.api.routes.get_token_count",
        side_effect=RuntimeError("token error"),
    ):
        response = client.post("/v1/messages/count_tokens", json=payload)

    assert response.status_code == 500
    assert "token error" in response.json()["detail"]


def test_stop_cli_with_messaging_workflow(client):
    session_control = MagicMock()
    session_control.stop_all = AsyncMock(return_value=StopResult(cancelled_count=3))
    services = app.state.services
    app.state.services = ApiServices(
        requests=services.requests,
        admin=services.admin,
        tasks=session_control,
    )

    response = client.post("/stop")

    assert response.status_code == 200
    assert response.json()["cancelled_count"] == 3
    session_control.stop_all.assert_awaited_once()


def test_stop_cli_fallback_to_manager(client):
    session_control = MagicMock()
    session_control.stop_all = AsyncMock(return_value=StopResult(source="cli_manager"))
    services = app.state.services
    app.state.services = ApiServices(
        requests=services.requests,
        admin=services.admin,
        tasks=session_control,
    )

    response = client.post("/stop")

    assert response.status_code == 200
    assert response.json()["source"] == "cli_manager"
    session_control.stop_all.assert_awaited_once()
