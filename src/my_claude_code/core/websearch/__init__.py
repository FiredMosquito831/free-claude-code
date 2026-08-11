"""Neutral web search contracts (no transport or config dependencies)."""

from .models import WebSearchResponse, WebSearchResultItem

__all__ = [
    "WebSearchResponse",
    "WebSearchResultItem",
]
