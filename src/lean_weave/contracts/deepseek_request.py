"""Pure DeepSeek request normalization shared by gateway and packing preview.

The gateway keeps feature/stage attribution, retry policy, transport and
journals.  This module owns only the deterministic mapping from an explicit
request to the effective wire shape that both layers count.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from lean_weave.contracts._types import ThinkingMode
from lean_weave.errors import UnsupportedLLMRequest

REASONING_EFFORTS = frozenset({"low", "medium", "high"})


@dataclass(frozen=True)
class ModelCapability:
    supports_thinking: bool = True
    supports_tools: bool = True
    supports_json_object: bool = True


#: Explicit local capability decision; no discovery call is made at runtime.
MODEL_CAPABILITIES: dict[str, ModelCapability] = {
    "deepseek-flash": ModelCapability(),
    "deepseek-v4-pro": ModelCapability(),
}


@dataclass(frozen=True)
class NormalizedChatRequest:
    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None
    response_format: dict[str, Any] | None
    result_model: type[Any] | None
    thinking: ThinkingMode
    reasoning_effort: str | None
    output_cap: int


@dataclass(frozen=True)
class PackedRequestGuard:
    """Optional in-process integrity guard for an explicitly packed request.

    This is not an authorization capability: it only detects accidental drift
    between a counted pack and the request handed to the gateway.
    """

    expected_fingerprint: str
    model: str
    output_cap: int
    input_tokens: int
    output_tokens: int
    total_tokens: int


def resolve_thinking(
    *,
    requested: Any,
    option: Any,
    configured: bool,
    capability: ModelCapability,
) -> ThinkingMode:
    selected = option if option is not None else requested
    if selected is None:
        selected = "enabled" if configured else "disabled"
    if selected not in {"enabled", "disabled"}:
        raise UnsupportedLLMRequest("Unsupported thinking mode")
    if selected == "enabled" and not capability.supports_thinking:
        raise UnsupportedLLMRequest("Model does not support thinking mode")
    return selected


def normalize_chat_request(
    *,
    model: str,
    messages: Any,
    tools: Any = None,
    response_format: Any = None,
    result_model: Any = None,
    thinking: ThinkingMode,
    reasoning_effort: str | None = None,
    output_cap: int,
    max_output_tokens: int | None = None,
) -> NormalizedChatRequest:
    """Validate and normalize one chat request without any I/O.

    ``max_output_tokens=None`` is the explicit offline preview mode: the
    caller's declared cap is validated as a positive integer but is not clamped
    to the gateway configuration.  The gateway always passes its ceiling.
    """

    if not isinstance(model, str) or not model.strip():
        raise UnsupportedLLMRequest("A non-empty model is required")
    capability = MODEL_CAPABILITIES.get(model)
    if capability is None:
        raise UnsupportedLLMRequest("Unsupported model")

    if isinstance(output_cap, bool) or not isinstance(output_cap, int) or output_cap <= 0:
        raise UnsupportedLLMRequest("Output token override must be a positive integer")
    if max_output_tokens is not None and output_cap > max_output_tokens:
        raise UnsupportedLLMRequest("Output token override exceeds the configured ceiling")

    if reasoning_effort is not None:
        if (
            not isinstance(reasoning_effort, str)
            or reasoning_effort not in REASONING_EFFORTS
        ):
            raise UnsupportedLLMRequest("Unsupported reasoning effort")
        if thinking != "enabled":
            raise UnsupportedLLMRequest("Reasoning effort requires thinking mode")

    normalized_format, normalized_result = _structured_mode(
        response_format, result_model
    )
    normalized_tools = _validate_tools(tools)
    if normalized_tools is not None and normalized_format is not None:
        raise UnsupportedLLMRequest("Tools and JSON object mode cannot be combined")
    if normalized_format is not None and not capability.supports_json_object:
        raise UnsupportedLLMRequest("Model does not support JSON object mode")
    if normalized_tools is not None and not capability.supports_tools:
        raise UnsupportedLLMRequest("Model does not support tools")

    normalized_messages = validate_messages(
        messages, thinking, normalized_tools is not None
    )
    return NormalizedChatRequest(
        model=model,
        messages=normalized_messages,
        tools=normalized_tools,
        response_format=normalized_format,
        result_model=normalized_result,
        thinking=thinking,
        reasoning_effort=reasoning_effort if thinking == "enabled" else None,
        output_cap=output_cap,
    )


def _structured_mode(
    response_format: Any, result_model: Any
) -> tuple[dict[str, Any] | None, type[Any] | None]:
    if result_model is not None and (
        not isinstance(result_model, type) or not hasattr(result_model, "model_validate")
    ):
        raise UnsupportedLLMRequest("Result validator must be a Pydantic model class")
    if response_format is None:
        if result_model is not None:
            raise UnsupportedLLMRequest("A result validator requires JSON object mode")
        return None, None
    if not isinstance(response_format, Mapping):
        raise UnsupportedLLMRequest("Unsupported response format")
    if response_format.get("type") != "json_object" or set(response_format) != {"type"}:
        raise UnsupportedLLMRequest("Only JSON object mode is supported")
    return {"type": "json_object"}, result_model


def _validate_tools(tools: Any) -> list[dict[str, Any]] | None:
    if tools is None:
        return None
    if not isinstance(tools, Sequence) or isinstance(tools, (str, bytes)):
        raise UnsupportedLLMRequest("Tools must be a sequence")
    if not tools:
        return None
    normalized: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, Mapping) or tool.get("type") != "function":
            raise UnsupportedLLMRequest("Only function tools are supported")
        function = tool.get("function")
        if not isinstance(function, Mapping):
            raise UnsupportedLLMRequest("Function tool schema is malformed")
        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            raise UnsupportedLLMRequest("Function tool name is required")
        normalized.append(copy.deepcopy(dict(tool)))
    return normalized


def validate_messages(
    messages: Any, thinking: ThinkingMode, has_tools: bool
) -> list[dict[str, Any]]:
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)) or not messages:
        raise UnsupportedLLMRequest("Explicit, non-empty messages are required")
    normalized: list[dict[str, Any]] = []
    call_ids: set[str] = set()
    for message in messages:
        if not isinstance(message, Mapping):
            raise UnsupportedLLMRequest("Messages must be mappings")
        role = message.get("role")
        if role not in {"system", "user", "assistant", "tool"}:
            raise UnsupportedLLMRequest("Unsupported message role")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise UnsupportedLLMRequest("Message content must be text or null")
        item: dict[str, Any] = {"role": role, "content": content}
        reasoning = message.get("reasoning_content")
        if reasoning is not None and not isinstance(reasoning, str):
            raise UnsupportedLLMRequest("Reasoning content must be text")
        if role == "assistant":
            tool_calls = message.get("tool_calls")
            if tool_calls is not None:
                calls = normalize_tool_calls(tool_calls)
                item["tool_calls"] = calls
                if thinking == "enabled" and not isinstance(reasoning, str):
                    raise UnsupportedLLMRequest(
                        "Thinking tool replay requires prior reasoning content"
                    )
                for call in calls:
                    call_ids.add(call["id"])
            if thinking == "enabled" and reasoning is not None:
                item["reasoning_content"] = reasoning
        elif role == "tool":
            tool_call_id = message.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                raise UnsupportedLLMRequest("Tool messages require a tool call ID")
            if thinking == "enabled" and has_tools and tool_call_id not in call_ids:
                raise UnsupportedLLMRequest("Tool result has no matching assistant tool call")
            item["tool_call_id"] = tool_call_id
        elif reasoning is not None:
            raise UnsupportedLLMRequest(
                "Reasoning content is only valid on assistant messages"
            )
        normalized.append(item)

    if thinking == "enabled" and has_tools:
        tool_ids = {
            item["tool_call_id"]
            for item in normalized
            if item.get("role") == "tool" and "tool_call_id" in item
        }
        if not call_ids.issubset(tool_ids):
            raise UnsupportedLLMRequest(
                "Thinking tool replay requires matching tool results"
            )
    return normalized


def normalize_tool_calls(tool_calls: Any) -> list[dict[str, Any]]:
    if not isinstance(tool_calls, Sequence) or isinstance(tool_calls, (str, bytes)):
        raise UnsupportedLLMRequest("Assistant tool calls must be a sequence")
    normalized: list[dict[str, Any]] = []
    for call in tool_calls:
        if not isinstance(call, Mapping):
            raise UnsupportedLLMRequest("Assistant tool call is malformed")
        identifier = call.get("id")
        function = call.get("function")
        if not isinstance(identifier, str) or not identifier:
            raise UnsupportedLLMRequest("Assistant tool call ID is required")
        if not isinstance(function, Mapping):
            raise UnsupportedLLMRequest("Assistant tool call function is malformed")
        name = function.get("name")
        arguments = function.get("arguments")
        if not isinstance(name, str) or not name:
            raise UnsupportedLLMRequest("Assistant tool call name is required")
        if isinstance(arguments, Mapping):
            arguments = json.dumps(arguments, separators=(",", ":"))
        if not isinstance(arguments, str):
            raise UnsupportedLLMRequest("Assistant tool call arguments must be text")
        normalized.append(
            {
                "id": identifier,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        )
    return normalized


def project_wire(normalized: NormalizedChatRequest) -> dict[str, Any]:
    """Project the normalized request to the exact SDK wire fields."""

    wire: dict[str, Any] = {
        "model": normalized.model,
        "messages": normalized.messages,
        "max_tokens": normalized.output_cap,
        "extra_body": {"thinking": {"type": normalized.thinking}},
    }
    if normalized.tools is not None:
        wire["tools"] = normalized.tools
    if normalized.response_format is not None:
        wire["response_format"] = normalized.response_format
    if normalized.reasoning_effort is not None:
        wire["reasoning_effort"] = normalized.reasoning_effort
    return wire


def wire_fingerprint(wire: Mapping[str, Any]) -> str:
    payload = json.dumps(
        wire,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
