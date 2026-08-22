"""Direct coverage for ``count_text_tokens``.

It was published in 5.42.0 so the local-optimization handlers could report an
honest reply length, and until now its behaviour was only asserted indirectly
through those handlers. The two properties below are the reasons it exists at
all, and neither is visible from a handler test.
"""

import pytest
import tiktoken

from my_claude_code.core.anthropic.models import Message
from my_claude_code.core.anthropic.tokens import count_text_tokens, get_token_count


class TestNoPerMessageFraming:
    """The reason this is not just ``get_token_count`` with one message.

    ``get_token_count`` bills a fixed overhead per message, which is correct
    for a request and wrong for a lone reply: it scores the empty string at 4.
    A handler that answers a request locally reports this number as
    ``usage.output_tokens``, so the overhead would be invented tokens.
    """

    def test_the_empty_string_costs_nothing(self):
        assert count_text_tokens("") == 0

    def test_get_token_count_does_charge_framing_for_the_same_text(self):
        # Pins the discrepancy that motivates the separate function, so nobody
        # "simplifies" one into the other.
        framed = get_token_count([Message(role="assistant", content="")], None, None)
        assert framed > count_text_tokens("")

    def test_a_reply_is_counted_as_its_own_text_only(self):
        text = "Conversation"
        expected = tiktoken.get_encoding("cl100k_base").encode(text)
        assert count_text_tokens(text) == len(expected)


class TestSpecialTokensDoNotRaise:
    """``_DISALLOWED_SPECIAL = ()`` is load-bearing, not decoration.

    tiktoken's default raises ValueError when the text contains a special
    token literal. Prompts are arbitrary user text and Claude Code transcripts
    routinely quote such strings, so the default would turn a countable
    request into a crash on the request path.
    """

    @pytest.mark.parametrize(
        "text",
        ["<|endoftext|>", "prefix <|endoftext|> suffix", "<|fim_prefix|>"],
    )
    def test_special_token_literals_are_counted_not_rejected(self, text):
        assert count_text_tokens(text) > 0


class TestMonotonicity:
    def test_longer_text_never_costs_fewer_tokens(self):
        counts = [count_text_tokens("word " * n) for n in (0, 1, 10, 100)]
        assert counts == sorted(counts)
        assert counts[0] == 0
        assert counts[-1] > counts[1]

    def test_unicode_is_counted_without_error(self):
        assert count_text_tokens("résumé 日本語 🎉") > 0
