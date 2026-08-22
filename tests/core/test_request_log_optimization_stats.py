"""The per-rule aggregate behind the Token Optimizer page.

Every fixture is written through the real ``RequestLogStore``, so the schema,
the writer thread and the column defaults are exercised rather than assumed. A
hand-written SQL double would encode what this test believed about the layout,
and would keep passing after the layout changed -- the double wins in CI and
loses in production.
"""

import pytest

from my_claude_code.core.request_log import (
    LOCAL_PROVIDER_PREFIX,
    UNKNOWN_PROVIDER_KEY,
    RequestLogStore,
    RequestRecord,
)

BASE = 1_700_000_000.0
DAY = 86_400.0


def _record(
    index: int,
    *,
    ts: float,
    provider: str | None = "p1",
    optimization: str | None = None,
    tokens_saved: int | None = None,
    tokens_in: int = 100,
    cache_read: int | None = None,
) -> RequestRecord:
    return RequestRecord(
        id=f"r{index:05d}",
        endpoint="/v1/messages",
        protocol="anthropic",
        ts_epoch=ts,
        provider=provider,
        resolved_model="m1",
        input_text="prompt",
        output_text="answer",
        tokens_in=tokens_in,
        tokens_out=10,
        cache_read_tokens=cache_read,
        optimization=optimization,
        optimization_tokens_saved=tokens_saved,
    )


def _store(tmp_path, records: list[RequestRecord]) -> RequestLogStore:
    store = RequestLogStore(tmp_path / "requests.db", max_rows=10_000)
    for record in records:
        store.enqueue(record)
    store.close(timeout=60.0)
    return RequestLogStore(tmp_path / "requests.db", max_rows=10_000)


@pytest.fixture
def seeded(tmp_path):
    records: list[RequestRecord] = []
    index = 0
    # Three days of title skips, a different count each day so a flat series
    # cannot pass for a real one.
    for day, count in enumerate((2, 5, 3)):
        for _ in range(count):
            index += 1
            records.append(
                _record(
                    index,
                    ts=BASE + day * DAY,
                    provider=None,
                    optimization="title_generation_skip",
                    tokens_saved=1_000,
                )
            )
    # One suggestion skip whose saving was never written down.
    index += 1
    records.append(
        _record(
            index,
            ts=BASE,
            provider=None,
            optimization="suggestion_mode_skip",
            tokens_saved=None,
        )
    )
    # Ordinary upstream traffic, which must not be counted as a saving.
    for _ in range(4):
        index += 1
        records.append(_record(index, ts=BASE, provider="p1"))
    store = _store(tmp_path, records)
    yield store
    store.close(timeout=60.0)


def test_totals_separate_local_answers_from_upstream_traffic(seeded) -> None:
    result = seeded.optimization_stats()

    assert result["total_requests"] == 15
    assert result["answered_locally"] == 11
    assert result["tokens_saved"] == 10_000


def test_each_rule_reports_its_own_fires_and_savings(seeded) -> None:
    rules = {row["rule"]: row for row in seeded.optimization_stats()["rules"]}

    assert rules["title_generation_skip"]["requests"] == 10
    assert rules["title_generation_skip"]["tokens_saved"] == 10_000
    assert rules["suggestion_mode_skip"]["requests"] == 1
    assert rules["suggestion_mode_skip"]["tokens_saved"] == 0


def test_an_unrecorded_saving_is_reported_as_unrecorded_not_as_zero(seeded) -> None:
    """`tokens_reported` is the whole point: 0 saved and 0 known are different."""
    rules = {row["rule"]: row for row in seeded.optimization_stats()["rules"]}

    assert rules["title_generation_skip"]["tokens_reported"] == 10
    # One fire, and nothing written down for it.
    assert rules["suggestion_mode_skip"]["requests"] == 1
    assert rules["suggestion_mode_skip"]["tokens_reported"] == 0


def test_the_daily_series_is_oldest_first_and_carries_real_counts(seeded) -> None:
    rules = {row["rule"]: row for row in seeded.optimization_stats()["rules"]}
    daily = rules["title_generation_skip"]["daily"]

    assert [point["requests"] for point in daily] == [2, 5, 3]
    assert [point["bucket"] for point in daily] == sorted(
        point["bucket"] for point in daily
    )
    assert sum(point["tokens_saved"] for point in daily) == 10_000


def test_the_series_is_bounded_to_the_requested_number_of_days(tmp_path) -> None:
    records = [
        _record(
            day,
            ts=BASE + day * DAY,
            provider=None,
            optimization="title_generation_skip",
            tokens_saved=7,
        )
        for day in range(20)
    ]
    store = _store(tmp_path, records)
    try:
        rules = store.optimization_stats(days=14)["rules"]
        assert len(rules[0]["daily"]) == 14
        # The bound keeps the NEWEST days: a sparkline showing the oldest
        # fortnight of a twenty-day log would be silently wrong.
        assert rules[0]["daily"][-1]["bucket"] > rules[0]["daily"][0]["bucket"]
        assert len(store.optimization_stats(days=3)["rules"][0]["daily"]) == 3
    finally:
        store.close(timeout=60.0)


def test_the_window_filters_the_aggregate(seeded) -> None:
    narrow = seeded.optimization_stats(since=BASE + DAY, until=BASE + DAY + 1)

    assert narrow["answered_locally"] == 5
    assert narrow["tokens_saved"] == 5_000


def test_an_empty_log_reports_zero_rules_rather_than_inventing_any(tmp_path) -> None:
    store = _store(tmp_path, [])
    try:
        result = store.optimization_stats()
        assert result["total_requests"] == 0
        assert result["answered_locally"] == 0
        assert result["tokens_saved"] == 0
        assert result["rules"] == []
    finally:
        store.close(timeout=60.0)


# ------------------------------------------------- the provider grouping key


def test_a_locally_answered_request_is_keyed_by_its_rule_not_by_unknown(
    seeded,
) -> None:
    keys = {row["key"] for row in seeded.stats()["by_provider"]}

    assert f"{LOCAL_PROVIDER_PREFIX}title_generation_skip" in keys
    assert f"{LOCAL_PROVIDER_PREFIX}suggestion_mode_skip" in keys
    assert UNKNOWN_PROVIDER_KEY not in keys


def test_a_row_with_no_provider_and_no_rule_is_still_honestly_unknown(
    tmp_path,
) -> None:
    store = _store(tmp_path, [_record(1, ts=BASE, provider=None)])
    try:
        keys = {row["key"] for row in store.stats()["by_provider"]}
        assert keys == {UNKNOWN_PROVIDER_KEY}
    finally:
        store.close(timeout=60.0)


def test_the_synthetic_key_is_a_working_filter_value(seeded) -> None:
    """A key a reader can see is a key a reader will type into the filter."""
    filtered = seeded.stats(provider=f"{LOCAL_PROVIDER_PREFIX}title_generation_skip")

    assert filtered["total"] == 10


def test_filtering_by_unknown_selects_only_genuinely_unattributed_rows(
    tmp_path,
) -> None:
    store = _store(
        tmp_path,
        [
            _record(1, ts=BASE, provider=None),
            _record(2, ts=BASE, provider=None, optimization="title_generation_skip"),
            _record(3, ts=BASE, provider="p1"),
        ],
    )
    try:
        assert store.stats(provider=UNKNOWN_PROVIDER_KEY)["total"] == 1
    finally:
        store.close(timeout=60.0)


def test_a_named_provider_and_a_rule_can_be_selected_together(seeded) -> None:
    both = seeded.stats(provider=f"p1,{LOCAL_PROVIDER_PREFIX}suggestion_mode_skip")

    assert both["total"] == 5


def test_an_ordinary_provider_filter_is_unchanged(seeded) -> None:
    assert seeded.stats(provider="p1")["total"] == 4
