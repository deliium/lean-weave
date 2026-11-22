"""Explicit adapter from a ready V2 pack to the existing DeepSeek request.

The adapter performs no transport call, retrieval, expansion or checkout
check.  It validates outbound scope and freezes a counted guard so the
gateway can reject accidental drift before booking or sending anything.
"""

from __future__ import annotations

from typing import Any, Iterable

from lean_weave.contracts._types import GatewayStage, LLMRequest
from lean_weave.contracts.deepseek_request import (
    MODEL_CAPABILITIES,
    PackedRequestGuard,
    normalize_chat_request,
    project_wire,
    wire_fingerprint,
)
from lean_weave.context.packing_models import PackedContextV2
from lean_weave.errors import InvalidContextRequest, UnsupportedLLMRequest
from lean_weave.logging_setup import get_logger

log = get_logger("context.packing_gateway")

_VALID_STAGES = frozenset({"plan", "implement", "verify", "retry"})


def prepare_packed_request(
    pack: PackedContextV2,
    *,
    feature_id: str,
    stage: GatewayStage,
    outbound_paths: Iterable[str],
    result_model: type[Any] | None = None,
) -> LLMRequest:
    """Return a normal ``LLMRequest`` with an optional packed guard.

    ``outbound_paths`` is the caller's explicit authorization for what may
    leave the process.  It must cover every source fragment and map row.
    """

    if not isinstance(pack, PackedContextV2) or not pack.dispatchable:
        raise InvalidContextRequest("Only a ready packed context can be dispatched")
    if not isinstance(feature_id, str) or not feature_id.strip():
        raise InvalidContextRequest("A non-empty feature ID is required")
    if stage not in _VALID_STAGES:
        raise InvalidContextRequest("A workflow stage is required for packed delivery")
    if feature_id == "__llm_probe__":
        raise InvalidContextRequest("The reserved probe feature cannot dispatch a pack")

    allowed = frozenset(outbound_paths)
    for fragment in pack.fragments:
        if fragment.path not in allowed:
            raise InvalidContextRequest("Packed fragment is outside the outbound scope")
    for row in pack.map_rows:
        if row.path not in allowed:
            raise InvalidContextRequest("Packed map row is outside the outbound scope")
    if pack.model not in MODEL_CAPABILITIES:
        raise UnsupportedLLMRequest("Unsupported model")

    try:
        normalized = normalize_chat_request(
            model=pack.model,
            messages=list(pack.messages),
            tools=list(pack.tools) or None,
            response_format=dict(pack.response_format)
            if pack.response_format
            else None,
            result_model=result_model,
            thinking=pack.thinking,  # type: ignore[arg-type]
            output_cap=pack.metrics.output_tokens,
            max_output_tokens=None,
        )
    except UnsupportedLLMRequest:
        raise InvalidContextRequest("Packed request envelope is not dispatchable") from None

    wire = project_wire(normalized)
    guard = PackedRequestGuard(
        expected_fingerprint=wire_fingerprint(wire),
        model=normalized.model,
        output_cap=normalized.output_cap,
        input_tokens=pack.metrics.input_tokens,
        output_tokens=pack.metrics.output_tokens,
        total_tokens=pack.metrics.total_tokens,
    )
    request: LLMRequest = {
        "feature_id": feature_id,
        "stage": stage,
        "model": pack.model,
        "messages": [dict(message) for message in pack.messages],
        "tools": list(pack.tools) or None,
        "thinking": pack.thinking,
        "response_format": dict(pack.response_format)
        if pack.response_format
        else None,
        "max_output_tokens": pack.metrics.output_tokens,
        "result_model": result_model,
        "packed_guard": guard,
    }
    log.debug(
        "[PackingGateway.prepare] fragments=%s map_rows=%s model=%s outbound=%s",
        len(pack.fragments),
        len(pack.map_rows),
        pack.model,
        len(allowed),
    )
    return request
