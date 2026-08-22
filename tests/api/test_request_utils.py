"""Tests for API request detection and token counting helpers."""

from unittest.mock import MagicMock

from my_claude_code.api.detection import is_title_generation_request
from my_claude_code.core.anthropic import get_token_count
from my_claude_code.core.anthropic.models import (
    Message,
    MessagesRequest,
    SystemContent,
)


class TestTitleGenerationRequest:
    """Tests for is_title_generation_request function."""

    def _title_gen_system(self) -> list[MagicMock]:
        block = MagicMock()
        block.text = (
            "Generate a concise, sentence-case title (3-7 words) that captures the "
            "main topic or goal of this coding session. Return JSON with a single "
            '"title" field.'
        )
        return [block]

    def test_title_generation_detected_via_system(self):
        """Title gen detected by session title system prompt (sentence-case / JSON)."""
        req = MagicMock(spec=MessagesRequest)
        req.system = self._title_gen_system()
        req.tools = None
        req.messages = []

        assert is_title_generation_request(req) is True

    def test_title_generation_not_detected_with_tools(self):
        """Not detected when tools are present (main conversation, not title gen)."""
        req = MagicMock(spec=MessagesRequest)
        req.system = self._title_gen_system()
        req.tools = [MagicMock()]
        req.messages = []

        assert is_title_generation_request(req) is False

    def test_title_generation_not_detected_no_system(self):
        """Not detected when system is absent."""
        req = MagicMock(spec=MessagesRequest)
        req.system = None
        req.tools = None
        req.messages = []

        assert is_title_generation_request(req) is False

    def test_title_generation_not_detected_unrelated_system(self):
        """Not detected when system prompt has no topic/title keywords."""
        block = MagicMock()
        block.text = "You are a helpful assistant."
        req = MagicMock(spec=MessagesRequest)
        req.system = [block]
        req.tools = None
        req.messages = []

        assert is_title_generation_request(req) is False

    def test_title_generation_return_json_coding_session_branch(self):
        """JSON title field + session wording matches without sentence-case phrase."""
        block = MagicMock()
        block.text = 'Return JSON with a single "title" field for this coding session.'
        req = MagicMock(spec=MessagesRequest)
        req.system = [block]
        req.tools = None
        req.messages = []

        assert is_title_generation_request(req) is True


class TestGetTokenCount:
    """Tests for get_token_count function."""

    def test_empty_messages(self):
        """Test token count with empty messages."""
        count = get_token_count([])
        assert count >= 1  # Returns max(1, tokens)

    def test_simple_message(self):
        """Test token count with simple text message."""
        msg = MagicMock()
        msg.content = "Hello world"

        count = get_token_count([msg])
        assert count > 0
        # "Hello world" is ~2-3 tokens plus overhead
        assert count >= 3

    def test_special_token_text_is_counted_as_plain_text(self):
        """Tiktoken special-token strings should not break token estimates."""
        msg = MagicMock()
        msg.content = "<|endoftext|>"

        count = get_token_count([msg], system="<|endoftext|>")
        assert count > 0

    def test_message_with_system_prompt(self):
        """Test token count includes system prompt."""
        msg = MagicMock()
        msg.content = "Hello"

        count = get_token_count([msg], system="You are a helpful assistant")
        assert count > 0

    def test_message_with_list_content(self):
        """Test token count with list content blocks."""
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Hello world"

        msg = MagicMock()
        msg.content = [text_block]

        count = get_token_count([msg])
        assert count > 0

    def test_message_with_thinking_block(self):
        """Test token count includes thinking blocks."""
        thinking_block = MagicMock()
        thinking_block.type = "thinking"
        thinking_block.thinking = "Let me think about this..."

        msg = MagicMock()
        msg.content = [thinking_block]

        count = get_token_count([msg])
        assert count > 0

    def test_message_with_tool_use(self):
        """Test token count includes tool use blocks."""
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.name = "search"
        tool_block.input = {"query": "test"}

        msg = MagicMock()
        msg.content = [tool_block]

        count = get_token_count([msg])
        assert count > 0

    def test_message_with_tool_result(self):
        """Test token count includes tool result blocks."""
        result_block = MagicMock()
        result_block.type = "tool_result"
        result_block.content = "Search results here"

        msg = MagicMock()
        msg.content = [result_block]

        count = get_token_count([msg])
        assert count > 0

    def test_message_with_tools(self):
        """Test token count includes tool definitions."""
        msg = MagicMock()
        msg.content = "Use the search tool"

        tool = MagicMock()
        tool.name = "search"
        tool.description = "Search for information"
        tool.input_schema = {"type": "object", "properties": {}}

        count = get_token_count([msg], tools=[tool])
        assert count > 0

    def test_system_as_list(self):
        """Test token count with system as list of blocks."""
        msg = MagicMock()
        msg.content = "Hello"

        block = MagicMock()
        block.text = "System prompt"

        count = get_token_count([msg], system=[block])
        assert count > 0

    def test_tool_result_with_dict_content(self):
        """Test token count with tool result containing dict content."""
        result_block = MagicMock()
        result_block.type = "tool_result"
        result_block.content = {"result": "data"}

        msg = MagicMock()
        msg.content = [result_block]

        count = get_token_count([msg])
        assert count > 0

    def test_multiple_messages_overhead(self):
        """Test that multiple messages include overhead."""
        msg1 = MagicMock()
        msg1.content = "Hi"

        msg2 = MagicMock()
        msg2.content = "Hello"

        count_single = get_token_count([msg1])
        count_double = get_token_count([msg1, msg2])

        # Double message should have more tokens (including overhead)
        assert count_double > count_single

    def test_inline_system_message_contributes_to_token_count(self):
        """Mid-conversation system content remains part of the counted transcript."""
        user_message = Message(role="user", content="Hello")
        inline_system = Message(role="system", content="New instructions")

        without_inline_system = get_token_count([user_message])
        with_inline_system = get_token_count([user_message, inline_system])

        assert with_inline_system > without_inline_system

    def test_per_message_overhead_four_tokens(self):
        """Per-message overhead is 4 tokens (was 3)."""
        msg = MagicMock()
        msg.content = "x"  # Minimal content
        count = get_token_count([msg])
        # 1 msg * 4 overhead + content tokens
        assert count >= 5

    def test_system_overhead_added(self):
        """System prompt adds ~4 tokens overhead."""
        msg = MagicMock()
        msg.content = "Hi"
        count_no_sys = get_token_count([msg])
        count_with_sys = get_token_count([msg], system="You are helpful")
        assert count_with_sys >= count_no_sys + 4

    def test_system_as_typed_content_blocks(self):
        """Typed system content blocks are counted."""
        msg = MagicMock()
        msg.content = "Hi"
        count_no_sys = get_token_count([msg])
        system_blocks = [
            SystemContent(type="text", text="System prompt from typed block")
        ]
        count_with_system = get_token_count([msg], system=system_blocks)
        assert count_with_system > count_no_sys

    def test_tool_use_includes_id(self):
        """Tool use blocks count id field."""
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.name = "search"
        tool_block.input = {"q": "test"}
        tool_block.id = "call_abc123"
        msg = MagicMock()
        msg.content = [tool_block]
        count = get_token_count([msg])
        assert count > 0

    def test_tool_result_includes_tool_use_id(self):
        """Tool result blocks count tool_use_id field."""
        result_block = MagicMock()
        result_block.type = "tool_result"
        result_block.content = "ok"
        result_block.tool_use_id = "call_xyz"
        msg = MagicMock()
        msg.content = [result_block]
        count = get_token_count([msg])
        assert count > 0

    def test_unrecognized_block_type_fallback(self):
        """Unrecognized block types are tokenized via json.dumps fallback."""
        unknown_block = {"type": "custom", "spec": "data"}
        msg = MagicMock()
        msg.content = [unknown_block]
        count = get_token_count([msg])
        assert count > 0

    def test_message_with_image_block(self):
        """Test token count includes image blocks."""
        image_block = MagicMock()
        image_block.type = "image"
        image_block.source = {
            "type": "base64",
            "media_type": "image/png",
            "data": "x" * 3000,
        }
        msg = MagicMock()
        msg.content = [image_block]
        count = get_token_count([msg])
        assert count >= 85

    def test_image_block_with_dict_source(self):
        """Image block with dict-style source is counted."""
        image_block = {"type": "image", "source": {"data": "a" * 10000}}
        msg = MagicMock()
        msg.content = [image_block]
        count = get_token_count([msg])
        assert count >= 85

    def test_known_payload_estimate_range(self):
        """Known payload produces estimate within expected range (validation harness)."""
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        system_text = "You are a helpful assistant."
        user_text = "Hello, how are you?"
        sys_tokens = len(enc.encode(system_text))
        user_tokens = len(enc.encode(user_text))
        # Min: content tokens + system overhead (4) + per-msg overhead (4)
        expected_min = sys_tokens + user_tokens + 4 + 4
        msg = MagicMock()
        msg.content = user_text
        count = get_token_count([msg], system=system_text)
        assert count >= expected_min, f"count={count} < expected_min={expected_min}"


# --- Parametrized Edge Case Tests ---
