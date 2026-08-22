"""Tests for api/optimization_handlers.py."""

from unittest.mock import patch

from my_claude_code.api.optimization_handlers import (
    OPTIMIZATION_HANDLERS,
    OPTIMIZATION_RULES,
    try_filepath_mock,
    try_optimizations,
    try_prefix_detection,
    try_quota_mock,
    try_suggestion_skip,
    try_title_skip,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import (
    ContentBlockText,
    Message,
    MessagesRequest,
)
from my_claude_code.core.anthropic.tokens import get_token_count


def _make_request(
    messages_content: str, max_tokens: int | None = None
) -> MessagesRequest:
    """Create a MessagesRequest with a single user message."""
    return MessagesRequest(
        model="claude-3-sonnet",
        max_tokens=max_tokens if max_tokens is not None else 100,
        messages=[Message(role="user", content=messages_content)],
    )


class TestTryPrefixDetection:
    def test_disabled_returns_none(self):
        settings = Settings()
        settings.fast_prefix_detection = False
        req = _make_request("x")
        with patch(
            "my_claude_code.api.optimization_handlers.is_prefix_detection_request",
            return_value=(True, "/ask"),
        ):
            assert try_prefix_detection(req, settings, get_token_count) is None

    def test_enabled_and_match_returns_response(self):
        settings = Settings()
        settings.fast_prefix_detection = True
        req = _make_request("x")
        with (
            patch(
                "my_claude_code.api.optimization_handlers.is_prefix_detection_request",
                return_value=(True, "/ask"),
            ),
            patch(
                "my_claude_code.api.optimization_handlers.extract_command_prefix",
                return_value="/ask",
            ),
            patch(
                "my_claude_code.api.optimization_handlers.logger.info"
            ) as mock_log_info,
        ):
            result = try_prefix_detection(req, settings, get_token_count)
        assert result is not None
        block = result.response.content[0]
        assert isinstance(block, ContentBlockText)
        assert block.text == "/ask"
        mock_log_info.assert_called_once_with(
            "Optimization: {} answered locally", "prefix_detection"
        )

    def test_enabled_but_no_match_returns_none(self):
        settings = Settings()
        settings.fast_prefix_detection = True
        req = _make_request("x")
        with patch(
            "my_claude_code.api.optimization_handlers.is_prefix_detection_request",
            return_value=(False, ""),
        ):
            assert try_prefix_detection(req, settings, get_token_count) is None


class TestTryQuotaMock:
    def test_disabled_returns_none(self):
        settings = Settings()
        settings.enable_network_probe_mock = False
        req = _make_request("quota", max_tokens=1)
        with patch(
            "my_claude_code.api.optimization_handlers.is_quota_check_request",
            return_value=True,
        ):
            assert try_quota_mock(req, settings, get_token_count) is None

    def test_enabled_and_match_returns_response(self):
        settings = Settings()
        settings.enable_network_probe_mock = True
        req = _make_request("quota", max_tokens=1)
        with patch(
            "my_claude_code.api.optimization_handlers.is_quota_check_request",
            return_value=True,
        ):
            result = try_quota_mock(req, settings, get_token_count)
        assert result is not None
        block = result.response.content[0]
        assert isinstance(block, ContentBlockText)
        assert "Quota check passed" in block.text


class TestTryTitleSkip:
    def test_disabled_returns_none(self):
        settings = Settings()
        settings.enable_title_generation_skip = False
        req = _make_request("write a 5-10 word title")
        with patch(
            "my_claude_code.api.optimization_handlers.is_title_generation_request",
            return_value=True,
        ):
            assert try_title_skip(req, settings, get_token_count) is None

    def test_enabled_and_match_returns_response(self):
        settings = Settings()
        settings.enable_title_generation_skip = True
        req = _make_request("x")
        with patch(
            "my_claude_code.api.optimization_handlers.is_title_generation_request",
            return_value=True,
        ):
            result = try_title_skip(req, settings, get_token_count)
        assert result is not None
        block = result.response.content[0]
        assert isinstance(block, ContentBlockText)
        assert block.text == "Conversation"


class TestTrySuggestionSkip:
    def test_disabled_returns_none(self):
        settings = Settings()
        settings.enable_suggestion_mode_skip = False
        req = _make_request("[SUGGESTION MODE: x]")
        with patch(
            "my_claude_code.api.optimization_handlers.is_suggestion_mode_request",
            return_value=True,
        ):
            assert try_suggestion_skip(req, settings, get_token_count) is None

    def test_enabled_and_match_returns_response(self):
        settings = Settings()
        settings.enable_suggestion_mode_skip = True
        req = _make_request("x")
        with patch(
            "my_claude_code.api.optimization_handlers.is_suggestion_mode_request",
            return_value=True,
        ):
            result = try_suggestion_skip(req, settings, get_token_count)
        assert result is not None
        block = result.response.content[0]
        assert isinstance(block, ContentBlockText)
        assert block.text == ""


class TestTryFilepathMock:
    def test_disabled_returns_none(self):
        settings = Settings()
        settings.enable_filepath_extraction_mock = False
        req = _make_request("Command:\nls\nOutput:\nfilepaths")
        with patch(
            "my_claude_code.api.optimization_handlers.is_filepath_extraction_request",
            return_value=(True, "ls", "out"),
        ):
            assert try_filepath_mock(req, settings, get_token_count) is None

    def test_enabled_and_match_returns_response(self):
        settings = Settings()
        settings.enable_filepath_extraction_mock = True
        req = _make_request("x")
        with (
            patch(
                "my_claude_code.api.optimization_handlers.is_filepath_extraction_request",
                return_value=(True, "ls", "a.txt b.txt"),
            ),
            patch(
                "my_claude_code.api.optimization_handlers.extract_filepaths_from_command",
                return_value="a.txt\nb.txt",
            ),
        ):
            result = try_filepath_mock(req, settings, get_token_count)
        assert result is not None
        block = result.response.content[0]
        assert isinstance(block, ContentBlockText)
        assert block.text == "a.txt\nb.txt"

    def test_extract_filepaths_empty_list_still_returns_response(self):
        settings = Settings()
        settings.enable_filepath_extraction_mock = True
        req = _make_request("x")
        with (
            patch(
                "my_claude_code.api.optimization_handlers.is_filepath_extraction_request",
                return_value=(True, "ls", "out"),
            ),
            patch(
                "my_claude_code.api.optimization_handlers.extract_filepaths_from_command",
                return_value="",
            ),
        ):
            result = try_filepath_mock(req, settings, get_token_count)
        assert result is not None
        block = result.response.content[0]
        assert isinstance(block, ContentBlockText)
        assert block.text == ""


class TestTryOptimizations:
    def test_first_match_wins(self):
        """Quota mock is first in OPTIMIZATION_HANDLERS; it should win over prefix."""
        settings = Settings()
        settings.enable_network_probe_mock = True
        settings.fast_prefix_detection = True
        req = _make_request("quota", max_tokens=1)
        with patch(
            "my_claude_code.api.optimization_handlers.is_quota_check_request",
            return_value=True,
        ):
            result = try_optimizations(req, settings, get_token_count)
        assert result is not None
        block = result.response.content[0]
        assert isinstance(block, ContentBlockText)
        assert "Quota check passed" in block.text

    def test_no_match_returns_none(self):
        settings = Settings()
        settings.fast_prefix_detection = False
        settings.enable_network_probe_mock = False
        settings.enable_title_generation_skip = False
        settings.enable_suggestion_mode_skip = False
        settings.enable_filepath_extraction_mock = False
        req = _make_request("random user message")
        assert try_optimizations(req, settings, get_token_count) is None


class TestUsageIsCountedNotInvented:
    """The reported usage used to be hardcoded regardless of the request."""

    def test_input_tokens_track_the_real_prompt_size(self):
        settings = Settings()
        settings.enable_title_generation_skip = True
        small = _make_request("hi")
        large = _make_request("word " * 4000)
        with patch(
            "my_claude_code.api.optimization_handlers.is_title_generation_request",
            return_value=True,
        ):
            small_result = try_title_skip(small, settings, get_token_count)
            large_result = try_title_skip(large, settings, get_token_count)

        assert small_result is not None and large_result is not None
        # The old implementation reported 100 for both of these.
        assert small_result.response.usage.input_tokens < 20
        assert large_result.response.usage.input_tokens > 3000
        assert (
            large_result.response.usage.input_tokens
            != small_result.response.usage.input_tokens
        )

    def test_tokens_saved_equals_the_prompt_that_never_went_upstream(self):
        settings = Settings()
        settings.enable_title_generation_skip = True
        req = _make_request("word " * 500)
        with patch(
            "my_claude_code.api.optimization_handlers.is_title_generation_request",
            return_value=True,
        ):
            result = try_title_skip(req, settings, get_token_count)
        assert result is not None
        assert result.tokens_saved == result.response.usage.input_tokens
        assert result.tokens_saved == get_token_count(req.messages, None, None)

    def test_output_tokens_track_the_reply_actually_returned(self):
        settings = Settings()
        settings.enable_suggestion_mode_skip = True
        settings.enable_title_generation_skip = True
        req = _make_request("x")
        with patch(
            "my_claude_code.api.optimization_handlers.is_suggestion_mode_request",
            return_value=True,
        ):
            empty = try_suggestion_skip(req, settings, get_token_count)
        with patch(
            "my_claude_code.api.optimization_handlers.is_title_generation_request",
            return_value=True,
        ):
            titled = try_title_skip(req, settings, get_token_count)
        assert empty is not None and titled is not None
        # "" against "Conversation": the old code reported 1 and 5 by fiat.
        assert empty.response.usage.output_tokens == 0
        assert titled.response.usage.output_tokens > 0


class TestRuleNames:
    def test_every_handler_reports_a_name_from_the_published_tuple(self):
        settings = Settings()
        settings.enable_title_generation_skip = True
        req = _make_request("x")
        with patch(
            "my_claude_code.api.optimization_handlers.is_title_generation_request",
            return_value=True,
        ):
            result = try_title_skip(req, settings, get_token_count)
        assert result is not None
        assert result.rule == "title_generation_skip"
        assert result.rule in OPTIMIZATION_RULES

    def test_published_tuple_covers_every_registered_handler(self):
        # A rule the tuple does not know about is a rule the dashboard cannot
        # name, which is how these went uncounted for their whole life.
        assert len(OPTIMIZATION_RULES) == len(OPTIMIZATION_HANDLERS)
