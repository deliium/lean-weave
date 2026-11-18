import pytest

from lean_weave.metrics.models import (
    UsageCounters,
    normalize_usage,
    token_usage_from_counters,
)


def test_normalizes_core_and_optional_fields():
    usage = normalize_usage(
        {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "prompt_cache_hit_tokens": 0,
            "prompt_tokens_details": {"cached_tokens": 4},
            "completion_tokens_details": {"reasoning_tokens": 3},
            "unknown_detail": {"value": None},
        }
    )
    assert usage is not None
    assert usage.counters == UsageCounters(
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        cache_hit_tokens=0,
        reasoning_tokens=3,
    )
    # An explicit zero preferred count must not fall back to the detail field.
    assert usage.counters.cache_hit_tokens == 0
    assert usage.raw["prompt_cache_hit_tokens"] == 0
    assert usage.raw["unknown_detail"] == {"value": None}
    assert usage.counters.core_complete()


def test_cache_falls_back_only_when_preferred_is_unavailable():
    usage = normalize_usage(
        {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
            "prompt_tokens_details": {"cached_tokens": 1},
        }
    )
    assert usage is not None
    assert usage.counters.cache_hit_tokens == 1
    assert usage.counters.reasoning_tokens is None


def test_missing_fields_are_not_measured_zero():
    usage = normalize_usage({"prompt_tokens": 4})
    assert usage is not None
    assert usage.counters.input_tokens == 4
    assert usage.counters.output_tokens is None
    assert usage.counters.total_tokens is None
    assert not usage.counters.core_complete()
    assert token_usage_from_counters(usage.counters).total_tokens == 0


def test_explicit_zero_survives():
    usage = normalize_usage(
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    )
    assert usage is not None
    assert usage.counters.total_tokens == 0
    assert usage.counters.core_complete()


@pytest.mark.parametrize("raw", [
    None,
    [],
    {"api_key": "secret"},
    {"nested": {"authorization": "Bearer value"}},
    {"value": object()},
    {"deep": {"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}},
])
def test_unusable_usage_is_rejected(raw):
    assert normalize_usage(raw) is None


@pytest.mark.parametrize("raw", [
    {"prompt_tokens": -1},
    {"prompt_tokens": True},
    {"prompt_tokens": "4"},
])
def test_malformed_counts_are_not_persisted_as_measured(raw):
    usage = normalize_usage(raw)
    assert usage is not None
    assert usage.counters.input_tokens is None
    assert not usage.counters.core_complete()
