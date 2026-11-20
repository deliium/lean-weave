"""Bounded stage-local LLM tool loop.

Every provider call is admitted by ``WorkflowBudget`` before dispatch. The
message buffer is ephemeral: it lives only for this loop, may echo
``reasoning_content`` within a thinking tool loop, and is dropped on exit.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from lean_weave.contracts import LLMProvider
from lean_weave.contracts._types import LLMMessage, LLMRequest
from lean_weave.errors import ProviderError, WorkflowBudgetExceeded
from lean_weave.logging_setup import get_logger
from lean_weave.schemas import TokenUsage
from lean_weave.workflow.budget import BudgetReservation, WorkflowBudget
from lean_weave.workflow.tools import ToolCallResult

log = get_logger("workflow.tool_loop")

Dispatch = Callable[[str, str | Mapping[str, Any] | None], ToolCallResult]


@dataclass(frozen=True)
class ToolEvent:
    name: str
    status: str
    committed: bool = False
    unconfirmed: bool = False


@dataclass(frozen=True)
class ToolLoopOutcome:
    content: str | None
    structured: Mapping[str, Any] | None
    rounds: int
    calls: int
    exhausted: bool = False
    overage: bool = False
    unconfirmed: bool = False
    cancelled: bool = False
    events: tuple[ToolEvent, ...] = field(default_factory=tuple)
    final_message: Mapping[str, Any] | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)


def _add_usage(total: TokenUsage, usage: object) -> TokenUsage:
    if hasattr(usage, "model_dump"):
        try:
            data = usage.model_dump(mode="json", exclude_none=True)
        except (TypeError, ValueError):
            return total
    elif isinstance(usage, Mapping):
        data = usage
    else:
        return total
    for field_name in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "reasoning_tokens",
        "prompt_cache_hit_tokens",
    ):
        value = data.get(field_name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            setattr(total, field_name, getattr(total, field_name) + value)
    return total


def _assistant_message(
    message: Mapping[str, Any], *, thinking: bool
) -> LLMMessage:
    assistant: LLMMessage = {"role": "assistant", "content": message.get("content")}
    tool_calls = message.get("tool_calls")
    if tool_calls:
        assistant["tool_calls"] = tool_calls
    if thinking and isinstance(message.get("reasoning_content"), str):
        assistant["reasoning_content"] = message["reasoning_content"]
    return assistant


def _tool_content(result: ToolCallResult) -> str:
    try:
        return json.dumps(result.content, separators=(",", ":"), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps({"ok": False, "status": "invalid"})


def run_tool_loop(
    provider: LLMProvider,
    budget: WorkflowBudget,
    *,
    feature_id: str,
    stage: str,
    scope: str,
    messages: Sequence[LLMMessage],
    tool_schemas: Sequence[Mapping[str, Any]] | None,
    dispatch: Dispatch | None,
    max_rounds: int,
    max_calls_per_batch: int,
    output_cap: int,
    model: str | None = None,
    thinking: str = "disabled",
    response_format: Mapping[str, Any] | None = None,
    reasoning_effort: str | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> ToolLoopOutcome:
    if max_rounds < 1:
        raise WorkflowBudgetExceeded("tool_round_limit")
    conversation: list[LLMMessage] = [dict(message) for message in messages]
    events: list[ToolEvent] = []
    calls = 0
    usage_total = TokenUsage()
    thinking_enabled = thinking == "enabled"

    for round_index in range(max_rounds):
        if cancel_check is not None and cancel_check():
            log.info(
                "[Workflow.loop] feature=%s scope=%s round=%s outcome=cancelled",
                feature_id,
                scope,
                round_index + 1,
            )
            return ToolLoopOutcome(
                content=None,
                structured=None,
                rounds=round_index,
                calls=calls,
                cancelled=True,
                events=tuple(events),
                usage=usage_total,
            )
        request: LLMRequest = {
            "feature_id": feature_id,
            "stage": stage,  # type: ignore[typeddict-item]
            "messages": conversation,
            "options": {"retry_limit": 0},
        }
        if model is not None:
            request["model"] = model
        if tool_schemas:
            request["tools"] = list(tool_schemas)
        if response_format is not None:
            request["response_format"] = dict(response_format)
        if thinking_enabled:
            request["thinking"] = "enabled"
        if reasoning_effort is not None and thinking_enabled:
            request["reasoning_effort"] = reasoning_effort

        reservation: BudgetReservation = budget.reserve(
            scope,
            messages=conversation,
            tools=tool_schemas,
            response_format=response_format,
            output_cap=output_cap,
        )
        operation_id = f"llm-{uuid.uuid4().hex}"
        budget.begin_effect(operation_id, scope)
        try:
            response = provider.complete(request)
        except Exception:
            # An uncertain attempt keeps its reservation and is not retried
            # automatically by a second workflow dispatch.
            budget.settle(reservation, None, reported=False)
            budget.end_effect(operation_id)
            log.warning(
                "[Workflow.loop] feature=%s scope=%s round=%s outcome=provider_error",
                feature_id,
                scope,
                round_index + 1,
            )
            raise
        calls += 1

        usage = response.get("usage")
        reported = bool(response.get("usage_reported", usage is not None))
        usage_total = _add_usage(usage_total, usage)
        try:
            budget.settle(reservation, usage, reported=reported)
        except WorkflowBudgetExceeded:
            budget.end_effect(operation_id)
            log.warning(
                "[Workflow.loop] feature=%s scope=%s round=%s outcome=overage",
                feature_id,
                scope,
                round_index + 1,
            )
            return ToolLoopOutcome(
                content=None,
                structured=None,
                rounds=round_index + 1,
                calls=calls,
                exhausted=True,
                overage=True,
                events=tuple(events),
                usage=usage_total,
            )
        budget.end_effect(operation_id)

        message = response.get("message") or {}
        if not isinstance(message, Mapping):
            raise ProviderError("Invalid provider message")
        conversation.append(_assistant_message(message, thinking=thinking_enabled))
        tool_calls = message.get("tool_calls")
        if not tool_calls:
            log.debug(
                "[Workflow.loop] feature=%s scope=%s rounds=%s calls=%s outcome=final",
                feature_id,
                scope,
                round_index + 1,
                calls,
            )
            return ToolLoopOutcome(
                content=message.get("content") if isinstance(message.get("content"), str) else None,
                structured=response.get("structured"),
                rounds=round_index + 1,
                calls=calls,
                events=tuple(events),
                usage=usage_total,
                final_message=message,
            )
        if dispatch is None:
            log.warning(
                "[Workflow.loop] feature=%s scope=%s outcome=tools_unavailable",
                feature_id,
                scope,
            )
            return ToolLoopOutcome(
                content=None,
                structured=None,
                rounds=round_index + 1,
                calls=calls,
                exhausted=True,
                events=tuple(events),
                usage=usage_total,
            )
        if not isinstance(tool_calls, Sequence) or isinstance(tool_calls, (str, bytes)):
            log.warning(
                "[Workflow.loop] feature=%s scope=%s outcome=invalid_tool_calls",
                feature_id,
                scope,
            )
            return ToolLoopOutcome(
                content=None,
                structured=None,
                rounds=round_index + 1,
                calls=calls,
                exhausted=True,
                events=tuple(events),
                usage=usage_total,
            )

        for index, call in enumerate(tool_calls):
            if not isinstance(call, Mapping):
                continue
            call_id = call.get("id")
            function = call.get("function")
            name = function.get("name") if isinstance(function, Mapping) else None
            arguments = function.get("arguments") if isinstance(function, Mapping) else None
            if not isinstance(call_id, str) or not isinstance(name, str):
                continue
            if cancel_check is not None and cancel_check():
                log.info(
                    "[Workflow.loop] feature=%s scope=%s outcome=cancelled_before_tool",
                    feature_id,
                    scope,
                )
                return ToolLoopOutcome(
                    content=None,
                    structured=None,
                    rounds=round_index + 1,
                    calls=calls,
                    cancelled=True,
                    events=tuple(events),
                    usage=usage_total,
                )
            if index >= max_calls_per_batch:
                result = ToolCallResult(
                    status="invalid",
                    content={
                        "ok": False,
                        "status": "invalid",
                        "guidance": "The batch call limit was reached; no action was taken.",
                    },
                )
            else:
                result = dispatch(name, arguments)
            events.append(
                ToolEvent(
                    name=name,
                    status=result.status,
                    committed=result.committed,
                    unconfirmed=result.unconfirmed,
                )
            )
            conversation.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": _tool_content(result),
                }
            )
            if result.unconfirmed:
                log.warning(
                    "[Workflow.loop] feature=%s scope=%s operation=%s outcome=committed_unconfirmed",
                    feature_id,
                    scope,
                    name,
                )
                return ToolLoopOutcome(
                    content=None,
                    structured=None,
                    rounds=round_index + 1,
                    calls=calls,
                    unconfirmed=True,
                    events=tuple(events),
                usage=usage_total,
                )

    log.warning(
        "[Workflow.loop] feature=%s scope=%s rounds=%s outcome=round_limit",
        feature_id,
        scope,
        max_rounds,
    )
    return ToolLoopOutcome(
        content=None,
        structured=None,
        rounds=max_rounds,
        calls=calls,
        exhausted=True,
        events=tuple(events),
        usage=usage_total,
    )
