from unittest.mock import MagicMock

from my_claude_code.api.detection import is_suggestion_mode_request
from my_claude_code.core.anthropic.models import Message, MessagesRequest


def _mk_req(messages, tools=None, system=None):
    req = MagicMock(spec=MessagesRequest)
    req.messages = messages
    req.tools = tools
    req.system = system
    return req


def _mk_msg(role: str, content):
    msg = MagicMock(spec=Message)
    msg.role = role
    msg.content = content
    return msg


class TestSuggestionMode:
    def test_detects_suggestion_mode_in_any_user_message(self):
        req = _mk_req(
            [
                _mk_msg("assistant", "ignore"),
                _mk_msg("user", "Hello\n[SUGGESTION MODE: on]\nworld"),
            ]
        )
        assert is_suggestion_mode_request(req) is True

    def test_suggestion_mode_ignores_non_user_messages(self):
        req = _mk_req([_mk_msg("assistant", "[SUGGESTION MODE: on]")])
        assert is_suggestion_mode_request(req) is False
