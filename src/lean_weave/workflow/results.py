"""Typed workflow result projection.

The durable control plane stays ``FeatureState`` plus the private checkpoint
record. This module only projects a bounded, read-only summary for callers; it
never owns authority and never stores prompts or chat history.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from lean_weave.schemas import (
    SCHEMA_VERSION,
    FeatureState,
    Stage,
    TaskStatus,
    TokenCounters,
    VerificationResult,
)

MAX_RESULT_TASKS = 200
MAX_REASON_CHARS = 64


class WorkflowStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class _ResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskResult(_ResultModel):
    id: str = Field(min_length=1, max_length=128)
    title: str = Field(default="", max_length=512)
    status: TaskStatus = TaskStatus.PENDING
    tokens: int = Field(default=0, ge=0)
    calls: int = Field(default=0, ge=0)


class WorkflowResult(_ResultModel):
    version: str = SCHEMA_VERSION
    feature_id: str
    status: WorkflowStatus
    reason: str = Field(default="", max_length=MAX_REASON_CHARS)
    stage: Stage = Stage.PLAN
    active_task_id: str | None = None
    tasks: list[TaskResult] = Field(default_factory=list)
    verification: VerificationResult | None = None
    counters: TokenCounters = Field(default_factory=TokenCounters)


def _reason(code: str | None, fallback: str) -> str:
    if not code:
        return fallback
    safe = "".join(char for char in code if 32 <= ord(char) < 127)
    return (safe or fallback)[:MAX_REASON_CHARS]


def project_result(state: FeatureState, control: object | None) -> WorkflowResult:
    """Project a bounded result from state and the private control record."""

    terminal_status = getattr(control, "terminal_status", None)
    terminal_reason = getattr(control, "terminal_reason", None)
    task_spend = getattr(control, "task_spend", {}) or {}

    if state.stage is Stage.DONE:
        status = WorkflowStatus.SUCCESS
        reason = _reason(terminal_reason, "completed")
    elif state.stage is Stage.FAILED:
        terminal_value = getattr(terminal_status, "value", terminal_status)
        if terminal_value == WorkflowStatus.BUDGET_EXHAUSTED.value:
            status = WorkflowStatus.BUDGET_EXHAUSTED
            reason = _reason(terminal_reason, "budget_exhausted")
        elif terminal_value == WorkflowStatus.CANCELLED.value:
            status = WorkflowStatus.CANCELLED
            reason = _reason(terminal_reason, "cancelled")
        else:
            status = WorkflowStatus.FAILED
            reason = _reason(terminal_reason, "failed")
    elif state.stage is Stage.INTERRUPTED:
        status = WorkflowStatus.INTERRUPTED
        reason = _reason(terminal_reason, "interrupted")
    elif terminal_status is not None:
        status = WorkflowStatus(getattr(terminal_status, "value", terminal_status))
        reason = _reason(terminal_reason, status.value)
    else:
        status = WorkflowStatus.INTERRUPTED
        reason = _reason(None, "run_in_progress")

    tasks: list[TaskResult] = []
    if state.plan is not None:
        for task in state.plan.tasks[:MAX_RESULT_TASKS]:
            spend = task_spend.get(task.id)
            tasks.append(
                TaskResult(
                    id=task.id,
                    title=task.title,
                    status=task.status,
                    tokens=getattr(spend, "tokens", 0),
                    calls=getattr(spend, "calls", 0),
                )
            )

    return WorkflowResult(
        feature_id=state.feature_id,
        status=status,
        reason=reason,
        stage=state.stage,
        active_task_id=state.active_task_id,
        tasks=tasks,
        verification=state.verification,
        counters=state.metrics,
    )
