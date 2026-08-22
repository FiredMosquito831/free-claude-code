"""Clustering the request log into recurring prompt families.

Every fixture here is written through the real ``RequestLogStore``, so the
compression, content-addressing and dictionary layers are exercised rather than
assumed. A hand-written SQL double would encode what this test believed about
the storage layout, and would keep passing after the layout changed.
"""

import time

import pytest

from my_claude_code.core.optimization_discovery import (
    BILLING_HEADER_MARKER,
    DEFAULT_SCAN_ROW_LIMIT,
    MAX_SCAN_ROW_LIMIT,
    MIN_FAMILY_REQUESTS,
    SIGNATURE_LINE_CHARS,
    discover_families,
    family_signature,
    strip_billing_header,
)
from my_claude_code.core.request_log import RequestLogStore, RequestRecord
from my_claude_code.providers.anthropic_oauth.entrypoint import (
    BILLING_HEADER_MARKER as PROVIDER_BILLING_HEADER_MARKER,
)

HEADER_LINE = "x-anthropic-billing-header: cc_version=2.1.235.2db; cc_entrypoint=cli;"
KEBAB_PROMPT = "Generate a short kebab-case name (2-4 words) that describes this.\nRespond with only the name."
TITLE_PROMPT = "Please write a 5-10 word title for the following conversation.\nRespond with the title only."


def _store(tmp_path, name: str = "requests.db") -> RequestLogStore:
    return RequestLogStore(tmp_path / name, max_rows=10_000)


def _seed(store: RequestLogStore, records: list[RequestRecord]) -> None:
    for record in records:
        store.enqueue(record)
    store.close(timeout=60.0)


def _record(
    index: int,
    prompt: str,
    *,
    base: float,
    optimization: str | None = None,
    tokens_in: int = 100,
    tokens_out: int = 10,
    tools_count: int = 0,
) -> RequestRecord:
    return RequestRecord(
        id=f"r{index:04d}",
        endpoint="/v1/messages",
        protocol="anthropic",
        ts_epoch=base + index,
        provider=None if optimization else "p1",
        resolved_model="m1",
        input_text=f"{HEADER_LINE}\n{prompt}",
        output_text="answer",
        input_chars=len(prompt),
        output_chars=6,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        params={"tools_count": tools_count},
        optimization=optimization,
        optimization_tokens_saved=tokens_in if optimization else None,
    )


# ------------------------------------------------------------------ signature


def test_billing_header_marker_matches_the_provider_that_owns_it() -> None:
    """``core`` may not import ``providers``, so the marker is repeated.

    Pinned in both directions: if Claude Code ever renames the attribution
    line and only the provider copy is updated, every signature here would
    silently regain a per-version first line and split one family across every
    client release that sent it.
    """
    assert BILLING_HEADER_MARKER == PROVIDER_BILLING_HEADER_MARKER


def test_signature_strips_the_attribution_line_so_client_versions_share_a_family() -> (
    None
):
    old = f"{HEADER_LINE}\n{KEBAB_PROMPT}"
    new = f"x-anthropic-billing-header: cc_version=9.9.9; cc_entrypoint=cli;\n{KEBAB_PROMPT}"

    assert family_signature(old) == family_signature(new)
    assert BILLING_HEADER_MARKER not in (family_signature(old) or "")


def test_signature_keeps_a_prompt_that_has_no_attribution_line_intact() -> None:
    assert strip_billing_header("hello\nworld") == "hello\nworld"
    assert family_signature("hello\nworld") == "hello\nworld"


def test_signature_takes_two_non_empty_lines_and_truncates_each() -> None:
    long_line = "A" * (SIGNATURE_LINE_CHARS + 50)
    text = f"{HEADER_LINE}\n\n  {long_line}  \n\nsecond\nthird\nfourth"

    signature = family_signature(text)

    assert signature == f"{'A' * SIGNATURE_LINE_CHARS}\nsecond"


def test_signature_is_none_without_prompt_text() -> None:
    assert family_signature(None) is None
    assert family_signature("") is None
    assert family_signature(f"{HEADER_LINE}\n   \n\n") is None


# ------------------------------------------------------------------- families


def test_empty_database_reports_nothing_and_says_it_scanned_nothing(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        result = discover_families(store, row_limit=DEFAULT_SCAN_ROW_LIMIT)
    finally:
        store.close(timeout=60.0)

    assert result["candidates"] == []
    assert result["covered"] == []
    assert result["distinct_signatures"] == 0
    assert result["scanned"]["rows"] == 0
    assert result["scanned"]["matching_rows"] == 0
    assert result["scanned"]["truncated"] is False
    assert result["scanned"]["newest_ts"] is None


def test_single_row_is_not_a_family(tmp_path) -> None:
    store = _store(tmp_path)
    _seed(store, [_record(0, KEBAB_PROMPT, base=time.time())])
    store = _store(tmp_path)
    try:
        result = discover_families(store, row_limit=DEFAULT_SCAN_ROW_LIMIT)
    finally:
        store.close(timeout=60.0)

    assert result["scanned"]["rows"] == 1
    assert result["distinct_signatures"] == 1
    assert result["recurring_families"] == 0
    assert result["candidates"] == []


def test_all_unique_prompts_produce_no_families(tmp_path) -> None:
    base = time.time()
    store = _store(tmp_path)
    _seed(store, [_record(i, f"unique prompt {i}", base=base) for i in range(12)])
    store = _store(tmp_path)
    try:
        result = discover_families(store, row_limit=DEFAULT_SCAN_ROW_LIMIT)
    finally:
        store.close(timeout=60.0)

    assert result["scanned"]["rows"] == 12
    assert result["distinct_signatures"] == 12
    assert result["recurring_families"] == 0
    assert result["candidates"] == []
    assert result["covered"] == []


def test_recurring_family_is_reported_with_its_measured_cost(tmp_path) -> None:
    base = 1_700_000_000.0
    store = _store(tmp_path)
    _seed(
        store,
        [
            _record(
                index,
                KEBAB_PROMPT,
                base=base,
                tokens_in=100 + index,
                tokens_out=5,
                tools_count=1 if index == 0 else 0,
            )
            for index in range(4)
        ],
    )
    store = _store(tmp_path)
    try:
        result = discover_families(store, row_limit=DEFAULT_SCAN_ROW_LIMIT)
    finally:
        store.close(timeout=60.0)

    assert len(result["candidates"]) == 1
    family = result["candidates"][0]
    assert family["signature"] == family_signature(KEBAB_PROMPT)
    assert family["requests"] == 4
    assert family["tokens_in"] == 100 + 101 + 102 + 103
    assert family["tokens_out"] == 20
    assert family["tokens_total"] == family["tokens_in"] + family["tokens_out"]
    assert family["requests_with_tools"] == 1
    assert family["first_seen"] == base
    assert family["last_seen"] == base + 3
    # A representative row so a human can open it in the analytics detail view.
    assert family["sample_request_id"] == "r0003"
    assert family["optimized_requests"] == 0


def test_families_rank_by_tokens_actually_spent(tmp_path) -> None:
    base = time.time()
    cheap = "Cheap recurring prompt\nsecond line"
    store = _store(tmp_path)
    _seed(
        store,
        [_record(i, cheap, base=base, tokens_in=1, tokens_out=1) for i in range(20)]
        + [
            _record(100 + i, KEBAB_PROMPT, base=base, tokens_in=5_000, tokens_out=10)
            for i in range(2)
        ],
    )
    store = _store(tmp_path)
    try:
        result = discover_families(store, row_limit=DEFAULT_SCAN_ROW_LIMIT)
    finally:
        store.close(timeout=60.0)

    signatures = [family["signature"] for family in result["candidates"]]
    assert signatures == [family_signature(KEBAB_PROMPT), family_signature(cheap)]


def test_a_family_a_live_rule_already_answers_is_not_a_candidate(tmp_path) -> None:
    """Already-solved is not a discovery.

    ``optimization`` is non-NULL exactly when a local rule answered the request
    without a provider. Such a family still has to be visible -- suppressing it
    entirely would make the rule that fires look identical to one that rotted
    -- so it is reported as covered instead.
    """
    base = time.time()
    store = _store(tmp_path)
    _seed(
        store,
        [
            _record(i, TITLE_PROMPT, base=base, optimization="title_generation_skip")
            for i in range(3)
        ]
        + [_record(100 + i, KEBAB_PROMPT, base=base) for i in range(3)],
    )
    store = _store(tmp_path)
    try:
        result = discover_families(store, row_limit=DEFAULT_SCAN_ROW_LIMIT)
    finally:
        store.close(timeout=60.0)

    candidates = [family["signature"] for family in result["candidates"]]
    covered = [family["signature"] for family in result["covered"]]
    assert candidates == [family_signature(KEBAB_PROMPT)]
    assert covered == [family_signature(TITLE_PROMPT)]
    assert result["covered"][0]["optimizations"] == ["title_generation_skip"]
    assert result["covered"][0]["optimized_requests"] == 3


def test_a_partly_covered_family_is_covered_not_a_candidate(tmp_path) -> None:
    base = time.time()
    store = _store(tmp_path)
    _seed(
        store,
        [_record(0, TITLE_PROMPT, base=base, optimization="title_generation_skip")]
        + [_record(i, TITLE_PROMPT, base=base) for i in range(1, 4)],
    )
    store = _store(tmp_path)
    try:
        result = discover_families(store, row_limit=DEFAULT_SCAN_ROW_LIMIT)
    finally:
        store.close(timeout=60.0)

    assert result["candidates"] == []
    assert len(result["covered"]) == 1
    assert result["covered"][0]["optimized_requests"] == 1
    assert result["covered"][0]["requests"] == 4


# ---------------------------------------------------------------------- bounds


def test_row_limit_is_honoured_and_the_response_says_it_sampled(tmp_path) -> None:
    base = time.time()
    store = _store(tmp_path)
    _seed(store, [_record(i, KEBAB_PROMPT, base=base) for i in range(30)])
    store = _store(tmp_path)
    try:
        result = discover_families(store, row_limit=10)
    finally:
        store.close(timeout=60.0)

    assert result["scanned"]["rows"] == 10
    assert result["scanned"]["row_limit"] == 10
    assert result["scanned"]["matching_rows"] == 30
    assert result["scanned"]["truncated"] is True
    assert result["candidates"][0]["requests"] == 10


def test_a_complete_scan_is_not_reported_as_truncated(tmp_path) -> None:
    base = time.time()
    store = _store(tmp_path)
    _seed(store, [_record(i, KEBAB_PROMPT, base=base) for i in range(5)])
    store = _store(tmp_path)
    try:
        result = discover_families(store, row_limit=DEFAULT_SCAN_ROW_LIMIT)
    finally:
        store.close(timeout=60.0)

    assert result["scanned"]["rows"] == 5
    assert result["scanned"]["matching_rows"] == 5
    assert result["scanned"]["truncated"] is False


def test_the_scan_reports_the_window_it_actually_covered(tmp_path) -> None:
    base = 1_700_000_000.0
    store = _store(tmp_path)
    _seed(store, [_record(i, KEBAB_PROMPT, base=base) for i in range(10)])
    store = _store(tmp_path)
    try:
        result = discover_families(store, row_limit=4)
    finally:
        store.close(timeout=60.0)

    # Newest-first, so a bounded scan covers the most recent traffic.
    assert result["scanned"]["newest_ts"] == base + 9
    assert result["scanned"]["oldest_ts"] == base + 6


def test_a_time_window_bounds_the_scan_and_is_echoed_back(tmp_path) -> None:
    base = 1_700_000_000.0
    store = _store(tmp_path)
    _seed(store, [_record(i, KEBAB_PROMPT, base=base) for i in range(10)])
    store = _store(tmp_path)
    try:
        result = discover_families(
            store, row_limit=DEFAULT_SCAN_ROW_LIMIT, since=base + 7
        )
    finally:
        store.close(timeout=60.0)

    assert result["scanned"]["rows"] == 3
    assert result["scanned"]["matching_rows"] == 3
    assert result["scanned"]["since"] == base + 7
    assert result["candidates"][0]["requests"] == 3


def test_row_limit_is_clamped_to_the_documented_ceiling(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        result = discover_families(store, row_limit=MAX_SCAN_ROW_LIMIT * 10)
        floor = discover_families(store, row_limit=0)
    finally:
        store.close(timeout=60.0)

    assert result["scanned"]["row_limit"] == MAX_SCAN_ROW_LIMIT
    assert floor["scanned"]["row_limit"] == 1


def test_min_requests_cannot_be_lowered_below_the_definition_of_recurring(
    tmp_path,
) -> None:
    base = time.time()
    store = _store(tmp_path)
    _seed(store, [_record(i, f"unique prompt {i}", base=base) for i in range(3)])
    store = _store(tmp_path)
    try:
        result = discover_families(
            store, row_limit=DEFAULT_SCAN_ROW_LIMIT, min_requests=1
        )
    finally:
        store.close(timeout=60.0)

    assert result["min_requests"] == MIN_FAMILY_REQUESTS
    assert result["candidates"] == []


def test_min_requests_can_be_raised(tmp_path) -> None:
    base = time.time()
    store = _store(tmp_path)
    _seed(
        store,
        [_record(i, KEBAB_PROMPT, base=base) for i in range(3)]
        + [_record(100 + i, TITLE_PROMPT, base=base) for i in range(5)],
    )
    store = _store(tmp_path)
    try:
        result = discover_families(
            store, row_limit=DEFAULT_SCAN_ROW_LIMIT, min_requests=4
        )
    finally:
        store.close(timeout=60.0)

    assert [family["signature"] for family in result["candidates"]] == [
        family_signature(TITLE_PROMPT)
    ]


def test_family_limit_truncates_and_says_so(tmp_path) -> None:
    base = time.time()
    store = _store(tmp_path)
    _seed(
        store,
        [
            _record(index * 10 + copy, f"family {index}\nsecond line", base=base)
            for index in range(5)
            for copy in range(2)
        ],
    )
    store = _store(tmp_path)
    try:
        result = discover_families(
            store, row_limit=DEFAULT_SCAN_ROW_LIMIT, family_limit=2
        )
    finally:
        store.close(timeout=60.0)

    assert result["recurring_families"] == 5
    assert len(result["candidates"]) == 2
    assert result["candidates_total"] == 5
    assert result["candidates_truncated"] is True


def test_rows_without_prompt_text_are_counted_not_guessed_at(tmp_path) -> None:
    base = time.time()
    store = _store(tmp_path)
    records = [_record(i, KEBAB_PROMPT, base=base) for i in range(2)]
    blank = _record(50, KEBAB_PROMPT, base=base)
    blank.input_text = None
    records.append(blank)
    _seed(store, records)
    store = _store(tmp_path)
    try:
        result = discover_families(store, row_limit=DEFAULT_SCAN_ROW_LIMIT)
    finally:
        store.close(timeout=60.0)

    assert result["scanned"]["rows"] == 3
    assert result["scanned"]["rows_without_prompt_text"] == 1
    assert result["candidates"][0]["requests"] == 2


@pytest.mark.parametrize("bound", [DEFAULT_SCAN_ROW_LIMIT, MAX_SCAN_ROW_LIMIT])
def test_documented_bounds_are_positive_and_ordered(bound: int) -> None:
    assert bound > 0
    assert DEFAULT_SCAN_ROW_LIMIT < MAX_SCAN_ROW_LIMIT
