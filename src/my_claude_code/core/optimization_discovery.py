"""Find recurring prompt families in the request log.

MCC answers a few Claude Code housekeeping requests inside the proxy instead of
paying a provider for them. Which requests those are is a hand-maintained list
mirroring somebody else's product, and it drifts in both directions: rules rot
when the request shape they targeted disappears, and new housekeeping families
appear with no rule for them. Neither direction was observable from inside the
codebase.

This module makes both observable. It clusters logged requests by a signature
taken from the head of the prompt, and reports each cluster with the tokens it
actually cost. It decides nothing: a family here is evidence for a human, never
a rule, and nothing in this module can cause a request to be answered locally.

Requests a live rule already answered carry a non-NULL ``optimization`` column,
and a family containing any of them is reported as covered rather than as a
candidate -- it is solved, not discovered.

Ranking is by measured tokens spent, descending. There is deliberately no
quality score: every number a caller might weigh is in the report, and the
weighing is the human's.
"""

import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from my_claude_code.core.request_log import RequestLogStore

# Claude Code stamps this attribution line at the head of the system prompt, so
# it is the first line of nearly every captured prompt and carries a client
# version that changes with every release. Left in, it would split one family
# across every version of Claude Code that ever sent it.
#
# ``core`` may not import ``providers`` (import-boundary contract), so the
# marker owned by ``providers.anthropic_oauth.entrypoint`` is repeated here.
# ``tests/core/test_optimization_discovery.py`` pins the two together.
BILLING_HEADER_MARKER = "x-anthropic-billing-header:"

# How much of the prompt head forms the signature. Two lines because a Claude
# Code housekeeping prompt states its whole instruction in its first line or
# two, and the rest is the payload being operated on -- which differs on every
# request and would make every request its own family.
SIGNATURE_LINES = 2
SIGNATURE_LINE_CHARS = 160

# The definition of "recurring", not a quality threshold: a family seen once is
# not a family. Callers may raise it; they may not lower it below 2.
MIN_FAMILY_REQUESTS = 2

# Scan bounds. Measured, not guessed: on a fixture of 20,000 rows carrying
# ~40 KB prompts through the real store, a cold scan sustained ~1,200 rows/s
# and a warm one ~5,200 rows/s, and decompression dominates both. The default
# is therefore ~1.6s cold on that fixture, and still a few seconds on the
# operator's 903 MB / 153,000-row log, whose full decompressing scan takes
# minutes. The ceiling is an opt-in: at the default it is not reachable.
DEFAULT_SCAN_ROW_LIMIT = 2_000
MAX_SCAN_ROW_LIMIT = 50_000

# Columns the scan projects. ``id``/``ts_epoch`` drive the store's keyset
# pagination, ``stream`` is required by its row decoder, and ``input_text`` is
# the only body column projected -- the reply is measured by its stored
# character count rather than decoded.
SCAN_COLUMNS = (
    "id",
    "ts_epoch",
    "stream",
    "tokens_in",
    "tokens_out",
    "input_chars",
    "output_chars",
    "params",
    "optimization",
    "input_text",
)


def strip_billing_header(text: str) -> str:
    """Drop the leading Claude Code attribution line, if the prompt has one."""
    stripped = text.lstrip()
    if not stripped.startswith(BILLING_HEADER_MARKER):
        return text
    _line, _, rest = stripped.partition("\n")
    return rest


def family_signature(
    input_text: str | None,
    *,
    lines: int = SIGNATURE_LINES,
    line_chars: int = SIGNATURE_LINE_CHARS,
) -> str | None:
    """Return the clustering key for one prompt, or None when there is no text.

    The first ``lines`` non-empty lines after the attribution line, each
    truncated. Truncation is what makes this cluster at all: a housekeeping
    prompt states its instruction first and then interpolates the thing being
    operated on, so an untruncated head is unique per request.
    """
    if not input_text:
        return None
    body = strip_billing_header(input_text)
    kept: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        kept.append(line[:line_chars])
        if len(kept) >= lines:
            break
    if not kept:
        return None
    return "\n".join(kept)


@dataclass(slots=True)
class _FamilyAccumulator:
    """Running totals for one signature. Mutable; ``report`` freezes it."""

    signature: str
    requests: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    input_chars: int = 0
    output_chars: int = 0
    output_chars_max: int = 0
    requests_with_tools: int = 0
    optimized_requests: int = 0
    optimizations: set[str] = field(default_factory=set)
    first_seen: float = 0.0
    last_seen: float = 0.0
    sample_request_id: str = ""

    def add(self, row: dict[str, Any]) -> None:
        self.requests += 1
        self.tokens_in += _as_int(row.get("tokens_in"))
        self.tokens_out += _as_int(row.get("tokens_out"))
        self.input_chars += _as_int(row.get("input_chars"))
        output_chars = _as_int(row.get("output_chars"))
        self.output_chars += output_chars
        self.output_chars_max = max(self.output_chars_max, output_chars)
        if _tools_count(row.get("params")) > 0:
            self.requests_with_tools += 1
        optimization = row.get("optimization")
        if optimization:
            self.optimized_requests += 1
            self.optimizations.add(str(optimization))
        timestamp = float(row.get("ts_epoch") or 0.0)
        # Rows arrive newest-first, so the first row seen is the last sent.
        if self.requests == 1:
            self.last_seen = timestamp
            self.sample_request_id = str(row.get("id") or "")
        self.first_seen = timestamp

    def report(self) -> dict[str, Any]:
        return {
            "signature": self.signature,
            "requests": self.requests,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens_total": self.tokens_in + self.tokens_out,
            "tokens_per_request": _mean(
                self.tokens_in + self.tokens_out, self.requests
            ),
            "input_chars_mean": _mean(self.input_chars, self.requests),
            "output_chars_mean": _mean(self.output_chars, self.requests),
            "output_chars_max": self.output_chars_max,
            "requests_with_tools": self.requests_with_tools,
            "optimized_requests": self.optimized_requests,
            "optimizations": sorted(self.optimizations),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "sample_request_id": self.sample_request_id,
        }


def discover_families(
    store: RequestLogStore,
    *,
    row_limit: int,
    since: float | None = None,
    until: float | None = None,
    min_requests: int = MIN_FAMILY_REQUESTS,
    family_limit: int = 50,
) -> dict[str, Any]:
    """Scan up to ``row_limit`` logged requests and cluster them into families.

    Newest-first, so a bounded scan covers the most recent traffic rather than
    an arbitrary slice. The returned ``scanned`` block states exactly what the
    scan covered and whether the bound cut it short: a sampled result must
    never be mistaken for a complete one.
    """
    row_limit = min(MAX_SCAN_ROW_LIMIT, max(1, row_limit))
    min_requests = max(MIN_FAMILY_REQUESTS, min_requests)
    family_limit = max(1, family_limit)

    started = time.perf_counter()
    _rows, matching = store.list_requests(
        limit=1, since=since, until=until, body_preview_chars=0
    )
    families: dict[str, _FamilyAccumulator] = {}
    scanned = 0
    without_signature = 0
    newest_ts: float | None = None
    oldest_ts: float | None = None
    for row in _iter_bounded(store, row_limit=row_limit, since=since, until=until):
        scanned += 1
        timestamp = float(row.get("ts_epoch") or 0.0)
        if newest_ts is None:
            newest_ts = timestamp
        oldest_ts = timestamp
        signature = family_signature(row.get("input_text"))
        if signature is None:
            # No prompt text: bodies were never captured, or the capture switch
            # was off when this row was written. Counted, never guessed at.
            without_signature += 1
            continue
        accumulator = families.get(signature)
        if accumulator is None:
            accumulator = _FamilyAccumulator(signature=signature)
            families[signature] = accumulator
        accumulator.add(row)

    recurring = [
        accumulator.report()
        for accumulator in families.values()
        if accumulator.requests >= min_requests
    ]
    # Ranked by tokens actually spent. Nothing here is scored: the ordering is
    # a measurement, and every number behind it is in the row.
    recurring.sort(key=lambda item: (-item["tokens_total"], -item["requests"]))
    candidates = [item for item in recurring if item["optimized_requests"] == 0]
    covered = [item for item in recurring if item["optimized_requests"] > 0]

    return {
        "scanned": {
            "rows": scanned,
            "row_limit": row_limit,
            "matching_rows": matching,
            # True when the bound stopped the scan before the window ran out,
            # i.e. these families describe a sample and not the whole window.
            "truncated": matching > scanned,
            "since": since,
            "until": until,
            "newest_ts": newest_ts,
            "oldest_ts": oldest_ts,
            "rows_without_prompt_text": without_signature,
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
        },
        "signature": {
            "billing_header_stripped": True,
            "lines": SIGNATURE_LINES,
            "line_chars": SIGNATURE_LINE_CHARS,
        },
        "min_requests": min_requests,
        "family_limit": family_limit,
        "distinct_signatures": len(families),
        "recurring_families": len(recurring),
        "candidates": candidates[:family_limit],
        "candidates_truncated": len(candidates) > family_limit,
        "candidates_total": len(candidates),
        "covered": covered[:family_limit],
        "covered_truncated": len(covered) > family_limit,
        "covered_total": len(covered),
    }


def _iter_bounded(
    store: RequestLogStore,
    *,
    row_limit: int,
    since: float | None,
    until: float | None,
) -> Iterator[dict[str, Any]]:
    """Yield at most ``row_limit`` rows from the store's keyset-paged export.

    Reads through the store's own accessor rather than querying the blob tables
    directly, so the compression and content-addressing layout stays the
    store's business. ``close()`` in ``finally`` releases the store's
    connection when the bound stops the scan early.
    """
    rows = store.iter_export_rows(
        columns=list(SCAN_COLUMNS),
        need_bodies=True,
        since=since,
        until=until,
        page_size=min(row_limit, 1_000),
    )
    try:
        for index, row in enumerate(rows):
            if index >= row_limit:
                return
            yield row
    finally:
        rows.close()


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return int(value)


def _mean(total: int, count: int) -> float:
    if count <= 0:
        return 0.0
    return round(total / count, 2)


def _tools_count(params: Any) -> int:
    if not isinstance(params, dict):
        return 0
    return _as_int(params.get("tools_count"))
