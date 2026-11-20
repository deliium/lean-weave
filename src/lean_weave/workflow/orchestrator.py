"""Minimal deterministic WorkflowOrchestrator.

The public protocol stays string-in/state-out. Durable checkpoints add the
private control record; stage functions stay outside this driver. A single
process owns one feature turn at a time, and no stage reads another stage's
messages.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from lean_weave.config.models import WorkflowConfig
from lean_weave.contracts import (
    ContextProvider,
    LLMProvider,
    StateStore,
    VerificationRunner,
)
from lean_weave.errors import (
    BudgetExceededError,
    DependencyUnavailable,
    PlanValidationError,
    ProviderError,
    StateConflict,
    WorkflowBudgetExceeded,
)
from lean_weave.logging_setup import get_logger
from lean_weave.schemas import FeatureState, Stage, TaskStatus
from lean_weave.verify.runner import repair_attribution
from lean_weave.workflow.budget import (
    FEATURE_TOKEN_OVERAGE,
    PLAN_SCOPE,
    BudgetLimits,
    WorkflowBudget,
)
from lean_weave.workflow.instructions import load_instructions
from lean_weave.workflow.planning import (
    PlanPolicy,
    SingleTaskSpec,
    build_plan_request,
    normalize_single_task_spec,
    parse_plan_response,
)
from lean_weave.workflow.results import WorkflowResult, WorkflowStatus, project_result
from lean_weave.workflow.stages import (
    pending_dependency_failure,
    ready_task,
    tasks_complete,
    terminal,
    transition,
)
from lean_weave.workflow.state import (
    ControlRecord,
    EffectKind,
    PendingEffect,
    checkpoint_capability,
    normalize_feature_id,
)
from lean_weave.workflow.tool_loop import ToolLoopOutcome, run_tool_loop
from lean_weave.workflow.tools import WorkflowTools

log = get_logger("workflow.orchestrator")

MAX_DRIVE_STEPS = 64
MAX_REASON_CHARS = 64


def _reason(code: str, fallback: str) -> str:
    safe = "".join(char for char in str(code) if 32 <= ord(char) < 127)
    return (safe or fallback)[:MAX_REASON_CHARS]


def _digest(outcome: ToolLoopOutcome) -> str:
    payload = "|".join(
        [outcome.content or "", *[f"{event.name}:{event.status}" for event in outcome.events]]
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class MinimalWorkflowOrchestrator:
    """Plan -> implement -> verify -> retry over durable FeatureState."""

    def __init__(
        self,
        *,
        config: WorkflowConfig,
        llm_provider: LLMProvider,
        context_provider: ContextProvider,
        state_store: StateStore,
        verification_runner: VerificationRunner,
        tools: WorkflowTools | None = None,
        plan_policy: PlanPolicy | None = None,
        model: str | None = None,
        thinking: str = "disabled",
        output_cap: int = 1024,
        instructions: Mapping[str, Sequence[str]] | None = None,
        journal: object | None = None,
    ) -> None:
        if not checkpoint_capability(state_store):
            raise DependencyUnavailable("StateStore cannot provide atomic workflow checkpoints")
        self._config = config
        self._provider = llm_provider
        self._context = context_provider
        self._store = state_store
        self._workspace = getattr(state_store, "identity", "sha256:" + "0" * 64)
        self._verification = verification_runner
        self._tools = tools
        self._plan_policy = plan_policy
        self._model = model
        self._thinking = thinking
        self._output_cap = output_cap
        self._instructions = dict(instructions or {})
        self._journal = journal
        self._state: FeatureState | None = None
        self._control: ControlRecord | None = None

    # ------------------------------------------------------------------
    # Public workflow contract
    # ------------------------------------------------------------------
    def run_feature(self, feature_id: str, request: str) -> FeatureState:
        self._require_policy()
        feature_id = normalize_feature_id(feature_id)
        state = FeatureState(
            feature_id=feature_id,
            stage=Stage.PLAN,
            request=request,
            max_retries=self._config.max_retries,
        )
        control = ControlRecord(feature_id=feature_id, workspace=self._workspace)
        self._store.create_checkpoint(state, control)  # type: ignore[attr-defined]
        self._state, self._control = state, control
        log.info("[Orchestrator.run] feature=%s path=detailed stage=plan", feature_id)
        return self._drive()

    def run_simple(
        self,
        feature_id: str,
        request: str,
        task_spec: SingleTaskSpec | Mapping[str, Any],
    ) -> FeatureState:
        policy = self._require_policy()
        plan = normalize_single_task_spec(request, task_spec, policy)
        feature_id = normalize_feature_id(feature_id)
        state = FeatureState(
            feature_id=feature_id,
            stage=Stage.IMPLEMENT,
            request=request,
            plan=plan,
            max_retries=self._config.max_retries,
        )
        control = ControlRecord(feature_id=feature_id, workspace=self._workspace)
        self._store.create_checkpoint(state, control)  # type: ignore[attr-defined]
        self._state, self._control = state, control
        log.info("[Orchestrator.run] feature=%s path=simple stage=implement", feature_id)
        return self._drive()

    def resume(self, feature_id: str, decision: Any | None = None) -> FeatureState:
        del decision
        envelope = self._store.load_checkpoint(normalize_feature_id(feature_id))  # type: ignore[attr-defined]
        self._state, self._control = envelope.state, envelope.control
        if self._control.pending_effect is not None:
            log.warning("[Orchestrator.resume] feature=%s outcome=unresolved_effect", feature_id)
            return self._interrupt("unresolved_effect")
        if self._state.stage in {Stage.DONE, Stage.FAILED}:
            log.info(
                "[Orchestrator.resume] feature=%s outcome=terminal stage=%s",
                feature_id,
                self._state.stage.value,
            )
            return self._state
        if self._state.stage is Stage.INTERRUPTED:
            reason = (self._state.interrupt or {}).get("reason")
            if reason in {"unresolved_effect", "committed_unconfirmed"}:
                log.warning("[Orchestrator.resume] feature=%s outcome=not_replayable", feature_id)
                return self._state
            refusal = self._approval_refusal()
            if refusal is not None:
                return self._interrupt(refusal)
            self._state.interrupt = None
            transition(self._state, "resume")
            self._control.terminal_status = None
            self._control.terminal_reason = None
            self._persist()
        else:
            refusal = self._approval_refusal()
            if refusal is not None:
                return self._interrupt(refusal)
        log.info(
            "[Orchestrator.resume] feature=%s stage=%s",
            feature_id,
            self._state.stage.value,
        )
        return self._drive()

    def result(self, feature_id: str) -> WorkflowResult:
        envelope = self._store.load_checkpoint(normalize_feature_id(feature_id))  # type: ignore[attr-defined]
        return project_result(envelope.state, envelope.control)

    # ------------------------------------------------------------------
    # Driver
    # ------------------------------------------------------------------
    def _drive(self) -> FeatureState:
        state = self._require_state()
        for _step in range(MAX_DRIVE_STEPS):
            if state.stage in {Stage.DONE, Stage.FAILED, Stage.INTERRUPTED}:
                return state
            if self._control is not None and self._control.pending_effect is not None:
                return self._interrupt("unresolved_effect")
            try:
                if state.stage is Stage.PLAN:
                    self._plan_stage()
                elif state.stage is Stage.IMPLEMENT:
                    self._implement_stage()
                elif state.stage is Stage.VERIFY:
                    self._verify_stage()
                elif state.stage is Stage.RETRY:
                    self._retry_stage()
                else:
                    return self._terminal(WorkflowStatus.FAILED, "unknown_stage")
            except WorkflowBudgetExceeded as exc:
                return self._budget_terminal(exc)
            except BudgetExceededError:
                log.warning(
                    "[Workflow.budget] feature=%s outcome=gateway_budget_denied",
                    state.feature_id,
                )
                return self._budget_terminal(WorkflowBudgetExceeded("gateway_budget_denied"))
            except PlanValidationError:
                return self._terminal(WorkflowStatus.FAILED, "invalid_plan")
            except ProviderError as exc:
                log.warning("[Orchestrator.stage] outcome=provider_error code=%s", _reason(str(exc), "provider_error"))
                return self._interrupt("provider_error")
            except StateConflict:
                return self._interrupt("state_error")
        log.warning("[Orchestrator.drive] feature=%s outcome=loop_bound", state.feature_id)
        return self._terminal(WorkflowStatus.FAILED, "loop_bound")

    # ------------------------------------------------------------------
    # Stage: plan
    # ------------------------------------------------------------------
    def _plan_stage(self) -> None:
        state = self._require_state()
        policy = self._require_policy()
        bundle = self._context.build("plan", state)
        fragments = load_instructions("plan", self._instructions.get("plan"))
        request = build_plan_request(
            bundle,
            instructions=fragments,
            approved_paths=sorted(policy.approved_paths),
        )
        outcome = run_tool_loop(
            self._provider,
            self._budget(),
            feature_id=state.feature_id,
            stage="plan",
            scope=PLAN_SCOPE,
            messages=request.messages,
            tool_schemas=None,
            dispatch=None,
            max_rounds=1,
            max_calls_per_batch=1,
            output_cap=self._output_cap,
            model=self._model,
            thinking=self._thinking,
            response_format=request.response_format,
        )
        if outcome.overage:
            self._accumulate_metrics(outcome)
            log.warning(
                "[Workflow.budget] feature=%s scope=plan outcome=overage code=%s",
                state.feature_id,
                FEATURE_TOKEN_OVERAGE,
            )
            raise WorkflowBudgetExceeded(FEATURE_TOKEN_OVERAGE)
        if outcome.exhausted:
            self._accumulate_metrics(outcome)
            self._terminal(WorkflowStatus.FAILED, "plan_call_unavailable")
            return
        self._accumulate_metrics(outcome)
        response = {
            "message": outcome.final_message
            or {"role": "assistant", "content": outcome.content},
            "structured": outcome.structured,
        }
        plan = parse_plan_response(response, policy)
        state.plan = plan
        state.active_task_id = None
        transition(state, "planned")
        self._persist()
        log.info(
            "[Orchestrator.plan] feature=%s tasks=%s calls=%s",
            state.feature_id,
            len(plan.tasks),
            outcome.calls,
        )

    # ------------------------------------------------------------------
    # Stage: implement
    # ------------------------------------------------------------------
    def _implement_stage(self) -> None:
        state = self._require_state()
        if state.plan is None:
            self._terminal(WorkflowStatus.FAILED, "missing_plan")
            return
        refusal = self._approval_refusal()
        if refusal is not None:
            self._terminal(WorkflowStatus.FAILED, refusal)
            return
        task = ready_task(state.plan, state.active_task_id)
        if task is None:
            if tasks_complete(state.plan):
                transition(state, "all_tasks_done")
                self._persist()
                log.info("[Orchestrator.implement] feature=%s outcome=all_tasks_done", state.feature_id)
                return
            if pending_dependency_failure(state.plan):
                self._terminal(WorkflowStatus.FAILED, "failed_dependency")
                return
            self._terminal(WorkflowStatus.FAILED, "no_ready_task")
            return
        state.active_task_id = task.id
        task.status = TaskStatus.IN_PROGRESS
        self._persist()
        log.info(
            "[Orchestrator.implement] feature=%s task=%s outcome=start",
            state.feature_id,
            task.id,
        )
        self._run_tool_stage(stage="implement", scope=task.id, task=task)

    # ------------------------------------------------------------------
    # Stage: verify
    # ------------------------------------------------------------------
    def _verify_stage(self) -> None:
        state = self._require_state()
        if state.plan is None:
            self._terminal(WorkflowStatus.FAILED, "missing_plan")
            return
        operation_id = f"check-{uuid.uuid4().hex}"
        self._begin_check_effect(operation_id, state.active_task_id)
        try:
            result = self._verification.run(state)
        except Exception:  # noqa: BLE001 - child failure becomes a stable code
            try:
                self._clear_effect(operation_id)
            except Exception:  # noqa: BLE001 - keep interrupt-safe state
                log.warning("[Orchestrator.verify] outcome=marker_clear_failed")
            log.warning("[Orchestrator.verify] feature=%s outcome=error", state.feature_id)
            self._terminal(WorkflowStatus.FAILED, "verification_error")
            return
        try:
            self._clear_effect(operation_id)
        except Exception:  # noqa: BLE001 - fail closed on unproven state
            log.warning("[Orchestrator.verify] outcome=marker_clear_failed")
            self._terminal(WorkflowStatus.FAILED, "state_error")
            return
        state.verification = result
        if result.status.value in {"pass", "warn"} and not result.blocking:
            transition(state, "pass")
            self._terminal(WorkflowStatus.SUCCESS, "verification_passed")
            return

        eligible = {path for task in state.plan.tasks for path in task.files}
        attribution = repair_attribution(result.blockers, eligible, result.affected_files)
        if attribution is None:
            self._terminal(WorkflowStatus.FAILED, "verification_failed")
            return
        candidates = [
            task
            for task in state.plan.tasks
            if set(task.files) & set(attribution.paths)
        ]
        if len(candidates) != 1:
            self._terminal(WorkflowStatus.FAILED, "ambiguous_diagnostics")
            return
        if state.retry_count >= state.max_retries:
            self._terminal(WorkflowStatus.FAILED, "retry_exhausted")
            return
        state.retry_count += 1
        active = candidates[0]
        active.status = TaskStatus.PENDING
        state.active_task_id = active.id
        transition(state, "fail")
        self._persist()
        log.warning(
            "[Orchestrator.retry] feature=%s task=%s retry=%s blockers=%s",
            state.feature_id,
            active.id,
            state.retry_count,
            len(result.blockers),
        )

    # ------------------------------------------------------------------
    # Stage: retry
    # ------------------------------------------------------------------
    def _retry_stage(self) -> None:
        state = self._require_state()
        if state.plan is None or state.verification is None or not state.active_task_id:
            self._terminal(WorkflowStatus.FAILED, "invalid_retry_state")
            return
        refusal = self._approval_refusal()
        if refusal is not None:
            self._terminal(WorkflowStatus.FAILED, refusal)
            return
        task = next(
            (item for item in state.plan.tasks if item.id == state.active_task_id),
            None,
        )
        if task is None:
            self._terminal(WorkflowStatus.FAILED, "invalid_retry_state")
            return
        task.status = TaskStatus.IN_PROGRESS
        self._persist()
        log.info(
            "[Orchestrator.retry] feature=%s task=%s outcome=start",
            state.feature_id,
            task.id,
        )
        self._run_tool_stage(stage="retry", scope=task.id, task=task)

    # ------------------------------------------------------------------
    # Shared tool stage
    # ------------------------------------------------------------------
    def _run_tool_stage(self, *, stage: str, scope: str, task: Any) -> None:
        state = self._require_state()
        tools = self._tools
        if tools is None:
            self._terminal(WorkflowStatus.FAILED, "approval_required")
            return
        bundle = self._context.build(stage, state)  # type: ignore[arg-type]
        fragments = load_instructions(stage, self._instructions.get(stage))
        messages = list(bundle.get("messages") or [])
        if fragments:
            messages = _append_trusted_instructions(messages, fragments)
        bound = tools.bind_task(task.files).with_effects(
            self._mark_patch_effect, self._clear_effect
        )
        outcome = run_tool_loop(
            self._provider,
            self._budget(),
            feature_id=state.feature_id,
            stage=stage,
            scope=scope,
            messages=messages,
            tool_schemas=bound.schemas(),
            dispatch=bound.dispatch,
            max_rounds=self._config.max_tool_rounds,
            max_calls_per_batch=self._config.max_tool_calls_per_batch,
            output_cap=self._output_cap,
            model=self._model,
            thinking=self._thinking,
        )
        self._accumulate_metrics(outcome)
        if outcome.overage:
            raise WorkflowBudgetExceeded(FEATURE_TOKEN_OVERAGE)
        if outcome.unconfirmed:
            effect = None
            if self._control is None or self._control.pending_effect is None:
                effect = PendingEffect(
                    kind=EffectKind.PATCH,
                    operation_id=f"patch-{uuid.uuid4().hex}",
                    task_id=task.id,
                    paths=list(task.files),
                    confirmed=False,
                )
            self._interrupt("committed_unconfirmed", effect=effect)
            return
        if outcome.exhausted:
            self._terminal(WorkflowStatus.FAILED, "task_loop_exhausted")
            return
        task.status = TaskStatus.DONE
        task.outcome_digest = _digest(outcome)
        state.active_task_id = None
        event = "task_done" if stage == "implement" else "requeue"
        transition(state, event)
        self._persist()
        log.info(
            "[Orchestrator.%s] feature=%s task=%s outcome=done calls=%s",
            stage,
            state.feature_id,
            task.id,
            outcome.calls,
        )

    # ------------------------------------------------------------------
    # Terminal and interrupt helpers
    # ------------------------------------------------------------------
    def _terminal(self, status: WorkflowStatus, reason: str) -> FeatureState:
        state = self._require_state()
        reason = _reason(reason, status.value)
        terminal(state, status, reason)
        if self._control is not None:
            self._control.terminal_status = status
            self._control.terminal_reason = reason
        self._persist()
        return state

    def _budget_terminal(self, exc: WorkflowBudgetExceeded) -> FeatureState:
        self._terminal(WorkflowStatus.BUDGET_EXHAUSTED, _reason(str(exc), "budget_exhausted"))
        return self._require_state()

    def _accumulate_metrics(self, outcome: ToolLoopOutcome) -> None:
        metrics = self._require_state().metrics
        metrics.prompt_tokens += outcome.usage.prompt_tokens
        metrics.completion_tokens += outcome.usage.completion_tokens
        metrics.reasoning_tokens += outcome.usage.reasoning_tokens
        metrics.prompt_cache_hit_tokens += outcome.usage.prompt_cache_hit_tokens
        metrics.llm_calls += outcome.calls
        log.debug(
            "[Orchestrator.metrics] feature=%s calls=%s total_tokens=%s",
            self._require_state().feature_id,
            metrics.llm_calls,
            metrics.prompt_tokens + metrics.completion_tokens,
        )

    def _interrupt(self, reason: str, *, effect: PendingEffect | None = None) -> FeatureState:
        state = self._require_state()
        state.stage = Stage.INTERRUPTED
        state.interrupt = {
            "reason": _reason(reason, "interrupted"),
            "task_id": state.active_task_id,
        }
        if self._control is not None:
            if effect is not None:
                self._control.pending_effect = effect
            self._control.terminal_status = WorkflowStatus.INTERRUPTED
            self._control.terminal_reason = _reason(reason, "interrupted")
        self._persist()
        log.warning(
            "[Orchestrator.interrupt] feature=%s reason=%s task=%s",
            state.feature_id,
            state.interrupt["reason"],
            state.active_task_id,
        )
        return state

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------
    def _require_state(self) -> FeatureState:
        if self._state is None:
            raise DependencyUnavailable("No feature is loaded")
        return self._state

    def _require_policy(self) -> PlanPolicy:
        if self._plan_policy is None:
            raise DependencyUnavailable("Caller approval and a plan policy are required")
        return self._plan_policy

    def _approval_refusal(self) -> str | None:
        if self._tools is None or not self._tools.approval.writes_approved:
            return "approval_required"
        preflight = getattr(self._verification, "preflight", None)
        if not callable(preflight):
            return "checks_unavailable"
        code = preflight()
        if code is not None:
            log.warning("[Orchestrator.preflight] outcome=refused code=%s", code)
            return code
        return None

    def _budget(self) -> WorkflowBudget:
        control = self._control
        if control is None:
            raise DependencyUnavailable("No feature control record is loaded")
        return WorkflowBudget(
            self._require_state().feature_id,
            control,
            limits=BudgetLimits.from_config(self._config),
            persist=self._persist_budget,
            journal=self._journal,
        )

    def _persist(self) -> None:
        self._store.save_checkpoint(self._require_state(), self._control)  # type: ignore[attr-defined]

    def _persist_budget(self, control: ControlRecord) -> None:
        self._control = control
        self._store.save_checkpoint(self._require_state(), control)  # type: ignore[attr-defined]

    def _begin_check_effect(self, operation_id: str, task_id: str | None) -> None:
        control = self._control
        if control is None:
            raise DependencyUnavailable("No feature control record is loaded")
        if control.pending_effect is not None:
            raise StateConflict("An effect is already pending for this feature")
        control.pending_effect = PendingEffect(
            kind=EffectKind.CHECK,
            operation_id=operation_id,
            task_id=task_id,
            confirmed=False,
        )
        self._persist()

    def _mark_patch_effect(
        self, operation_id: str, revision: str | None, paths: list[str]
    ) -> None:
        control = self._control
        if control is None:
            raise DependencyUnavailable("No feature control record is loaded")
        if control.pending_effect is not None:
            raise StateConflict("An effect is already pending for this feature")
        control.pending_effect = PendingEffect(
            kind=EffectKind.PATCH,
            operation_id=operation_id,
            task_id=self._require_state().active_task_id,
            revision=revision,
            paths=list(paths),
            confirmed=False,
        )
        self._persist()

    def _clear_effect(self, operation_id: str) -> None:
        control = self._control
        if control is None:
            raise DependencyUnavailable("No feature control record is loaded")
        pending = control.pending_effect
        if pending is None:
            return
        if pending.operation_id != operation_id:
            raise StateConflict("Pending effect identity does not match")
        control.pending_effect = None
        self._persist()


def _append_trusted_instructions(messages: list[Any], fragments: Sequence[str]) -> list[Any]:
    if not messages or messages[0].get("role") != "system":
        return messages
    block = "\n".join(f"- {text}" for text in fragments)
    updated = dict(messages[0])
    updated["content"] = f"{updated.get('content', '')}\n\nPROJECT METHOD FRAGMENTS:\n{block}"
    return [updated, *messages[1:]]
