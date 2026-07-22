"""Neutral web search result contracts shared by adapters and integrations.

This module is part of ``core``: stdlib-only, no transport (httpx/aiohttp) and
no ``config`` imports (see contract tests).
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WebSearchResultItem:
    """One normalized search result independent of provider wire format."""

    title: str
    url: str
    snippet: str  # "" when absent
    content: str | None  # fuller text when the provider returns it
    published: str | None  # ISO date when known


@dataclass(frozen=True, slots=True)
class WebSearchResponse:
    """Normalized response for one web search call."""

    provider: str  # provider_id
    query: str
    results: tuple[WebSearchResultItem, ...]
    key_index: int  # which key served it (0-based)
    cost_usd: float | None
