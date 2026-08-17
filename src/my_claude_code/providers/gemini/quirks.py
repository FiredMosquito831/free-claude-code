"""Gemini request quirks compatibility shim."""

from my_claude_code.providers.google_openai import (
    GOOGLE_SKIP_THOUGHT_SIGNATURE_VALIDATOR as GEMINI_SKIP_THOUGHT_SIGNATURE_VALIDATOR,
)

__all__ = ["GEMINI_SKIP_THOUGHT_SIGNATURE_VALIDATOR"]
