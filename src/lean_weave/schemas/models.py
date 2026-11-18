"""Pydantic models for FeatureState and related entities."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0.0"

class FeatureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feature_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    title: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    prompt_cache_hit_tokens: int = Field(default=0, ge=0)

class Stage(str, Enum):
    PLAN = "plan"
    IMPLEMENT = "implement"
    VERIFY = "verify"
    RETRY = "retry"
    DONE = "done"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class VerificationStatus(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class ImplementationTask(BaseModel):
    id: str
    title: str
    description: str = ""
    files: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    outcome_digest: str | None = None


class FeaturePlan(BaseModel):
    version: str = SCHEMA_VERSION
    goal: str
    settings: dict[str, Any] = Field(
        default_factory=lambda: {
            "testing": True,
            "logging": "verbose",
            "docs": True,
        }
    )
    reconciliation: list[dict[str, str]] = Field(default_factory=list)
    tasks: list[ImplementationTask] = Field(default_factory=list)
    commit_checkpoints: list[dict[str, Any]] = Field(default_factory=list)


class VerificationResult(BaseModel):
    version: str = SCHEMA_VERSION
    status: VerificationStatus
    blocking: bool = False
    blockers: list[dict[str, Any]] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    gate_summaries: list[str] = Field(default_factory=list)
    suggested_next: str | None = None


class TokenCounters(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    prompt_cache_hit_tokens: int = 0
    llm_calls: int = 0


class FeatureState(BaseModel):
    """Structured workflow state — independent of full chat history."""

    version: str = SCHEMA_VERSION
    feature_id: str
    stage: Stage = Stage.PLAN
    request: str = ""
    plan: FeaturePlan | None = None
    active_task_id: str | None = None
    verification: VerificationResult | None = None
    retry_count: int = 0
    max_retries: int = 2
    interrupt: dict[str, Any] | None = None
    metrics: TokenCounters = Field(default_factory=TokenCounters)
    # Explicitly forbidden: messages/chat history fields


# Transition table for minimal SM orchestrator
TRANSITIONS: dict[tuple[Stage, str], Stage] = {
    (Stage.PLAN, "planned"): Stage.IMPLEMENT,
    (Stage.IMPLEMENT, "task_done"): Stage.IMPLEMENT,
    (Stage.IMPLEMENT, "all_tasks_done"): Stage.VERIFY,
    (Stage.IMPLEMENT, "pause"): Stage.INTERRUPTED,
    (Stage.INTERRUPTED, "resume"): Stage.IMPLEMENT,
    (Stage.VERIFY, "pass"): Stage.DONE,
    (Stage.VERIFY, "fail"): Stage.RETRY,
    (Stage.RETRY, "requeue"): Stage.IMPLEMENT,
    (Stage.RETRY, "exhausted"): Stage.FAILED,
}
