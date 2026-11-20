"""Review-gate and resume-decision helpers for the minimal orchestrator.

Lifecycle metadata lives on the private checkpoint control record. These
helpers are pure: they never call a provider, never write files, and never
grant tool authority. Binding callbacks are injected by the session layer.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from collections.abc import Callable, Mapping
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lean_weave.logging_setup import get_logger
from lean_weave.schemas import FeaturePlan, ImplementationTask, Stage
from lean_weave.workflow.planning import PlanPolicy

log = get_logger("workflow.lifecycle")


class ExecutionMode(str, Enum):
    AUTO = "auto"
    REVIEW = "review"


class _LifeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewGate(_LifeModel):
    """Durable pre-implementation gate. Token plaintext is optional and local."""

    plan_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    binding_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    token_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    baseline_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    accepted: bool = False
    token: str | None = Field(default=None, max_length=128)


class ContinuationCursor(_LifeModel):
    """Last validated safe stage and task progress for boundary-safe resume."""

    stage: Stage
    active_task_id: str | None = None
    task_status: dict[str, str] = Field(default_factory=dict)


class ResumeDecision(_LifeModel):
    """Typed resume decision consumed by ``MinimalWorkflowOrchestrator.resume``."""

    kind: Literal["approve_review"] = "approve_review"
    token: str = Field(min_length=8, max_length=128)


BaselineFn = Callable[[], str]
BindingFn = Callable[[], Mapping[str, str]]


def digest_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def digest_text(text: str) -> str:
    return digest_bytes(text.encode("utf-8"))


def canonical_plan_projection(plan: FeaturePlan) -> dict[str, Any]:
    """Immutable plan projection used for digests (excludes mutable task fields)."""

    tasks: list[dict[str, Any]] = []
    for task in plan.tasks:
        tasks.append(
            {
                "id": task.id,
                "title": task.title,
                "description": task.description,
                "files": list(task.files),
                "depends_on": list(task.depends_on),
            }
        )
    return {
        "version": plan.version,
        "goal": plan.goal,
        "settings": plan.settings,
        "reconciliation": plan.reconciliation,
        "commit_checkpoints": plan.commit_checkpoints,
        "tasks": tasks,
    }


def plan_digest(plan: FeaturePlan) -> str:
    payload = json.dumps(
        canonical_plan_projection(plan),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return digest_bytes(payload)


def policy_material(policy: PlanPolicy) -> dict[str, Any]:
    return {
        "approved_paths": sorted(policy.approved_paths),
        "approved_check_ids": sorted(policy.approved_check_ids),
        "max_tasks": policy.max_tasks,
        "bypass_allowed": policy.bypass_allowed,
        "require_bypass_checks": policy.require_bypass_checks,
    }


def binding_digest(
    *,
    feature_id: str,
    plan: FeaturePlan,
    policy: PlanPolicy,
    workspace: str,
    head: str,
    baseline_fingerprint: str,
    extra: Mapping[str, str] | None = None,
) -> str:
    material = {
        "feature_id": feature_id,
        "plan_digest": plan_digest(plan),
        "policy": policy_material(policy),
        "workspace": workspace,
        "head": head,
        "baseline_fingerprint": baseline_fingerprint,
        "extra": {key: extra[key] for key in sorted(extra or {})},
    }
    payload = json.dumps(material, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return digest_bytes(payload)


def new_review_token() -> str:
    return secrets.token_hex(16)


def token_hash(token: str) -> str:
    return digest_text(token)


def publish_review_gate(
    *,
    feature_id: str,
    plan: FeaturePlan,
    policy: PlanPolicy,
    workspace: str,
    head: str,
    baseline_fingerprint: str,
    extra: Mapping[str, str] | None = None,
) -> ReviewGate:
    token = new_review_token()
    gate = ReviewGate(
        plan_digest=plan_digest(plan),
        binding_digest=binding_digest(
            feature_id=feature_id,
            plan=plan,
            policy=policy,
            workspace=workspace,
            head=head,
            baseline_fingerprint=baseline_fingerprint,
            extra=extra,
        ),
        token_hash=token_hash(token),
        baseline_fingerprint=baseline_fingerprint,
        accepted=False,
        token=token,
    )
    log.info("[Workflow.lifecycle] op=review_pause feature=%s", feature_id)
    log.debug("[Workflow.lifecycle] op=review_gate outcome=published")
    return gate


def validate_review_approval(
    gate: ReviewGate,
    *,
    token: str,
    feature_id: str,
    plan: FeaturePlan,
    policy: PlanPolicy,
    workspace: str,
    head: str,
    baseline_fingerprint: str,
    extra: Mapping[str, str] | None = None,
) -> str | None:
    """Return a stable refusal code, or None when the approval is valid."""

    if gate.accepted:
        log.warning("[Workflow.lifecycle] outcome=review_already_accepted")
        return "review_already_accepted"
    if token_hash(token) != gate.token_hash:
        log.warning("[Workflow.lifecycle] outcome=invalid_review_token")
        return "invalid_review_token"
    current_plan = plan_digest(plan)
    if current_plan != gate.plan_digest:
        log.warning("[Workflow.lifecycle] outcome=stale_plan")
        return "stale_plan"
    if baseline_fingerprint != gate.baseline_fingerprint:
        log.warning("[Workflow.lifecycle] outcome=stale_baseline")
        return "stale_baseline"
    current_binding = binding_digest(
        feature_id=feature_id,
        plan=plan,
        policy=policy,
        workspace=workspace,
        head=head,
        baseline_fingerprint=baseline_fingerprint,
        extra=extra,
    )
    if current_binding != gate.binding_digest:
        log.warning("[Workflow.lifecycle] outcome=stale_binding")
        return "stale_binding"
    log.debug("[Workflow.lifecycle] op=review_validate outcome=ok")
    return None


def parse_resume_decision(decision: Any) -> ResumeDecision | None:
    if decision is None:
        return None
    if isinstance(decision, ResumeDecision):
        return decision
    try:
        if isinstance(decision, Mapping):
            return ResumeDecision.model_validate(dict(decision))
        return ResumeDecision.model_validate(decision)
    except (ValidationError, TypeError, ValueError):
        log.warning("[Workflow.lifecycle] outcome=invalid_decision")
        return None


def capture_continuation(state_stage: Stage, active_task_id: str | None, tasks: list[ImplementationTask]) -> ContinuationCursor:
    return ContinuationCursor(
        stage=state_stage,
        active_task_id=active_task_id,
        task_status={task.id: task.status.value for task in tasks},
    )


def issue_correlation_id() -> str:
    return uuid.uuid4().hex
