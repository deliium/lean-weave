"""Shared typing aliases for contracts (no runtime logic)."""

from __future__ import annotations

from typing import Any, Literal, Mapping, Sequence, TypedDict

StageName = Literal["plan", "implement", "verify", "retry"]
ThinkingMode = Literal["enabled", "disabled"]


class TokenUsage(TypedDict, total=False):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    reasoning_tokens: int
    prompt_cache_hit_tokens: int


class LLMMessage(TypedDict, total=False):
    role: str
    content: str | None
    reasoning_content: str | None
    tool_calls: Sequence[Mapping[str, Any]] | None
    tool_call_id: str


class LLMRequest(TypedDict, total=False):
    model: str
    messages: Sequence[LLMMessage]
    tools: Sequence[Mapping[str, Any]] | None
    thinking: ThinkingMode
    reasoning_effort: str | None
    response_format: Mapping[str, Any] | None
    stage: StageName


class LLMResponse(TypedDict, total=False):
    message: LLMMessage
    usage: TokenUsage
    raw_finish_reason: str | None


class ContextBundle(TypedDict, total=False):
    stage: StageName
    system_prompt: str
    user_prompt: str
    file_excerpts: Mapping[str, str]
    digests: Mapping[str, str]
    estimated_tokens: int


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
