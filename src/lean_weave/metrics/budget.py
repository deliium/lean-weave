"""Conservative local token estimation and budget booking policy."""

from __future__ import annotations

import json
import math
from typing import Any

from lean_weave.metrics.models import ReportedUsage

#: A conservative local approximation: three UTF-8 characters per token plus
#: fixed envelope overhead. It intentionally overestimates normal English/code
#: text rather than undercharging the feature budget.
CHARS_PER_TOKEN = 3
ENVELOPE_OVERHEAD = 64


def estimate_tokens(value: Any) -> int:
    try:
        encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)
    except (TypeError, ValueError, RecursionError):
        encoded = repr(value)
    return max(1, math.ceil(len(encoded.encode("utf-8")) / CHARS_PER_TOKEN)) + ENVELOPE_OVERHEAD


def estimate_request_tokens(
    *,
    messages: Any,
    tools: Any = None,
    response_format: Any = None,
    extra: Any = None,
) -> int:
    """Estimate serialized provider-bound input, including schemas and replay."""
    return estimate_tokens(
        {"messages": messages, "tools": tools, "response_format": response_format, "extra": extra}
    )


def effective_output_cap(requested: int | None, configured: int) -> int:
    cap = configured if requested is None else requested
    if cap <= 0:
        raise ValueError("output cap must be positive")
    if cap > configured:
        raise ValueError("output cap cannot exceed configured maximum")
    return cap


def reservation_charge(estimated_input_tokens: int, output_cap: int) -> int:
    return max(0, estimated_input_tokens) + max(0, output_cap)


def charge_for_usage(
    usage: ReportedUsage | None, *, reserved: int
) -> tuple[int, bool]:
    """Return ``(charge, measured)`` for budget spend.

    If all three core counters are valid and internally consistent, the
    reported total is the charge. Optional cache/reasoning breakdowns never
    change it. Otherwise the greater of the reservation and the known lower
    bound is charged, and the charge is explicitly not measured.
    """
    if usage is None:
        return max(0, reserved), False
    counters = usage.counters
    if counters.core_complete():
        return int(counters.total_tokens or 0), True
    lower_bound = max(
        counters.total_tokens or 0,
        (counters.input_tokens or 0) + (counters.output_tokens or 0),
    )
    return max(reserved, lower_bound), False
