"""Shared typing aliases for contracts (no runtime logic)."""

from __future__ import annotations

from typing import Any, Literal, Mapping, Sequence, TypedDict

from pydantic import BaseModel

from lean_weave.schemas.models import TokenUsage

StageName = Literal["plan", "implement", "verify", "retry"]
GatewayStage = Literal["plan", "implement", "verify", "retry", "probe"]
ThinkingMode = Literal["enabled", "disabled"]

#: Reserved attribution for the explicit, opt-in connection check. Normal
#: workflow stages cannot use this feature ID, and the probe cannot use a
#: normal workflow stage.
PROBE_FEATURE_ID = "__llm_probe__"
PROBE_STAGE: GatewayStage = "probe"


class LLMMessage(TypedDict, total=False):
    role: str
    content: str | None
    reasoning_content: str | None
    tool_calls: Sequence[Mapping[str, Any]] | None
    tool_call_id: str


class LLMGatewayOptions(TypedDict, total=False):
    """Per-call gateway overrides. They only tighten configured limits."""

    retry_limit: int
    thinking: ThinkingMode
    max_output_tokens: int


class LLMRequest(TypedDict, total=False):
    feature_id: str
    model: str
    messages: Sequence[LLMMessage]
    tools: Sequence[Mapping[str, Any]] | None
    thinking: ThinkingMode
    reasoning_effort: str | None
    response_format: Mapping[str, Any] | None
    stage: GatewayStage
    max_output_tokens: int | None
    result_model: type[BaseModel] | None
    options: LLMGatewayOptions
    #: Optional local integrity guard for an explicitly packed request.
    packed_guard: Any


class LLMResponse(TypedDict, total=False):
    message: LLMMessage
    usage: TokenUsage
    usage_reported: bool
    raw_finish_reason: str | None
    model: str | None
    structured: Mapping[str, Any] | None



class ContextBundle(TypedDict, total=False):
    stage: StageName
    system_prompt: str
    user_prompt: str
    #: The canonical prompt pair is the only caller-facing message surface.
    messages: list[LLMMessage]
    file_excerpts: Mapping[str, str]
    digests: Mapping[str, str]
    estimated_tokens: int
    #: Typed inspection view; never appended to ``messages`` a second time.
    pack: Any


class ExecRequest(TypedDict, total=False):
    argv: Sequence[str]
    cwd: str
    timeout_s: float
    allow_network: bool


class ExecResult(TypedDict, total=False):
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
