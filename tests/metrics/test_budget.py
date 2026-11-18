import pytest

from lean_weave.metrics.budget import (
    charge_for_usage,
    effective_output_cap,
    estimate_request_tokens,
    estimate_tokens,
    reservation_charge,
)
from lean_weave.metrics.models import normalize_usage


def test_estimate_is_conservative_and_includes_schemas():
    small = estimate_request_tokens(messages=[{"role": "user", "content": "hi"}])
    with_tool = estimate_request_tokens(
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "echo", "parameters": {"type": "object"}}}],
    )
    assert small > len("hi")
    assert with_tool > small
    assert estimate_tokens("abcdef" * 100) >= 200


def test_output_cap_can_only_lower_ceiling():
    assert effective_output_cap(None, 100) == 100
    assert effective_output_cap(32, 100) == 32
    with pytest.raises(ValueError):
        effective_output_cap(101, 100)
    with pytest.raises(ValueError):
        effective_output_cap(0, 100)


def test_reservation_charge_adds_effective_output_cap():
    assert reservation_charge(10, 20) == 30


def test_measured_charge_uses_reported_total():
    usage = normalize_usage(
        {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "completion_tokens_details": {"reasoning_tokens": 3},
        }
    )
    assert charge_for_usage(usage, reserved=100) == (15, True)


def test_missing_optional_breakdowns_does_not_make_usage_incomplete():
    usage = normalize_usage(
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    )
    assert charge_for_usage(usage, reserved=100) == (15, True)


def test_incomplete_usage_charges_greater_of_reservation_and_lower_bound():
    usage = normalize_usage({"prompt_tokens": 10, "completion_tokens": 5})
    assert charge_for_usage(usage, reserved=100) == (100, False)
    assert charge_for_usage(usage, reserved=2) == (15, False)


def test_absent_usage_charges_reservation():
    assert charge_for_usage(None, reserved=42) == (42, False)
