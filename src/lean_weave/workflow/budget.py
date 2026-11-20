"""Stage-local admission and settlement for workflow LLM calls.

Admission binds a conservative input-plus-output reservation in the durable
checkpoint *before* dispatch. Settlement replaces it with verified usage when
complete; otherwise the reservation (or the known lower bound) stays charged.
Calls and tokens are cumulative per planning scope or task id across retries
and resumes. A crash after booking never refunds and never redispatches.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from lean_weave.config.models import WorkflowConfig
from lean_weave.errors import StateConflict, WorkflowBudgetExceeded
from lean_weave.logging_setup import get_logger
from lean_weave.metrics.budget import (
    estimate_request_tokens,
    reservation_charge,
)
from lean_weave.metrics.models import ReportedUsage, UsageCounters, normalize_usage
from lean_weave.workflow.state import (
    ControlRecord,
    EffectKind,
    PendingEffect,
    ScopeSpend,
    TaskSpend,
)

log = get_logger("workflow.budget")

PLAN_SCOPE = "plan"
PLAN_CALL_LIMIT = "plan_call_limit"
PLAN_TOKEN_LIMIT = "plan_token_limit"
TASK_CALL_LIMIT = "task_call_limit"
TASK_TOKEN_LIMIT = "task_token_limit"
FEATURE_TOKEN_LIMIT = "feature_token_limit"
FEATURE_TOKEN_OVERAGE = "feature_token_overage"


@dataclass(frozen=True)
class BudgetLimits:
    max_plan_calls: int
    max_plan_tokens: int
    max_task_calls: int
    max_task_tokens: int
    feature_tokens: int

    @classmethod
    def from_config(cls, config: WorkflowConfig) -> "BudgetLimits":
        return cls(
            max_plan_calls=config.max_plan_calls,
            max_plan_tokens=config.max_plan_tokens,
            max_task_calls=config.max_task_llm_calls,
            max_task_tokens=config.max_task_tokens,
            feature_tokens=config.feature_token_budget,
        )


@dataclass(frozen=True)
class BudgetReservation:
    scope: str
    charge: int
    estimated_input_tokens: int
    output_cap: int


@dataclass(frozen=True)
class UsageCharge:
    charge: int
    measured: bool


def _counters_from_usage(usage: object) -> tuple[UsageCounters | None, bool]:
    if usage is None:
        return None, False
    if isinstance(usage, ReportedUsage):
        return usage.counters, True
    if hasattr(usage, "model_dump"):
        try:
            raw = usage.model_dump(mode="json", exclude_none=True)
        except (TypeError, ValueError):
            return None, False
    elif isinstance(usage, Mapping):
        raw = dict(usage)
    else:
        return None, False
    normalized = normalize_usage(raw)
    if normalized is None:
        return None, False
    counters = normalized.counters
    if (counters.total_tokens or 0) == 0 and (counters.input_tokens or 0) == 0 and (
        counters.output_tokens or 0
    ) == 0:
        return counters, False
    return counters, counters.core_complete()


def settle_charge(
    usage: object,
    *,
    reserved: int,
    reported: bool = True,
) -> UsageCharge:
    """Return ``(charge, measured)`` without inventing measured zeros."""

    counters, complete = _counters_from_usage(usage)
    if not reported or counters is None:
        return UsageCharge(max(0, reserved), False)
    if complete:
        return UsageCharge(int(counters.total_tokens or 0), True)
    lower_bound = max(
        counters.total_tokens or 0,
        (counters.input_tokens or 0) + (counters.output_tokens or 0),
    )
    return UsageCharge(max(reserved, lower_bound), False)


class WorkflowBudget:
    """Mutable admission policy over one durable control record."""

    def __init__(
        self,
        feature_id: str,
        control: ControlRecord,
        *,
        limits: BudgetLimits,
        persist: Callable[[ControlRecord], None] | None = None,
        journal: object | None = None,
    ) -> None:
        if control.feature_id != feature_id:
            raise WorkflowBudgetExceeded("Budget identity does not match the feature")
        self.feature_id = feature_id
        self.control = control
        self.limits = limits
        self._persist = persist
        self._journal = journal

    # ------------------------------------------------------------------
    # Scope and feature spend
    # ------------------------------------------------------------------
    def _scope(self, scope: str) -> ScopeSpend:
        if scope == PLAN_SCOPE:
            return self.control.plan_spend
        spend = self.control.task_spend.get(scope)
        if spend is None:
            spend = TaskSpend()
            self.control.task_spend[scope] = spend
        return spend

    def _scope_limits(self, scope: str) -> tuple[int, int]:
        if scope == PLAN_SCOPE:
            return self.limits.max_plan_calls, self.limits.max_plan_tokens
        return self.limits.max_task_calls, self.limits.max_task_tokens

    def feature_spend(self) -> int:
        spent = getattr(self._journal, "spent", None)
        if callable(spent):
            try:
                return int(spent(self.feature_id))
            except (TypeError, ValueError):
                log.warning("[Workflow.budget] feature=%s outcome=ledger_unreadable", self.feature_id)
        local = self.control.plan_spend.tokens + sum(
            spend.tokens for spend in self.control.task_spend.values()
        )
        return local

    def scope_spend(self, scope: str) -> tuple[int, int]:
        spend = self._scope(scope)
        return spend.calls, spend.tokens

    def _save(self) -> None:
        if self._persist is not None:
            self._persist(self.control)

    # ------------------------------------------------------------------
    # Admission and settlement
    # ------------------------------------------------------------------
    def reserve(
        self,
        scope: str,
        *,
        messages: Any,
        tools: Any = None,
        response_format: Any = None,
        output_cap: int,
        extra: Any = None,
    ) -> BudgetReservation:
        if output_cap <= 0:
            raise WorkflowBudgetExceeded("output_cap_invalid")
        estimated = estimate_request_tokens(
            messages=messages,
            tools=tools,
            response_format=response_format,
            extra=extra,
        )
        charge = reservation_charge(estimated, output_cap)
        spend = self._scope(scope)
        call_limit, token_limit = self._scope_limits(scope)
        if spend.calls + 1 > call_limit:
            log.warning(
                "[Workflow.budget] feature=%s scope=%s outcome=refused code=%s calls=%s",
                self.feature_id,
                scope,
                PLAN_CALL_LIMIT if scope == PLAN_SCOPE else TASK_CALL_LIMIT,
                spend.calls,
            )
            raise WorkflowBudgetExceeded(
                PLAN_CALL_LIMIT if scope == PLAN_SCOPE else TASK_CALL_LIMIT
            )
        if spend.tokens + charge > token_limit:
            code = PLAN_TOKEN_LIMIT if scope == PLAN_SCOPE else TASK_TOKEN_LIMIT
            log.warning(
                "[Workflow.budget] feature=%s scope=%s outcome=refused code=%s spent=%s requested=%s limit=%s",
                self.feature_id,
                scope,
                code,
                spend.tokens,
                charge,
                token_limit,
            )
            raise WorkflowBudgetExceeded(code)
        if self.feature_spend() + charge > self.limits.feature_tokens:
            log.warning(
                "[Workflow.budget] feature=%s scope=%s outcome=refused code=%s requested=%s",
                self.feature_id,
                scope,
                FEATURE_TOKEN_LIMIT,
                charge,
            )
            raise WorkflowBudgetExceeded(FEATURE_TOKEN_LIMIT)

        spend.calls += 1
        spend.tokens += charge
        self._save()
        log.debug(
            "[Workflow.budget] feature=%s scope=%s outcome=reserved call=%s charge=%s spent=%s",
            self.feature_id,
            scope,
            spend.calls,
            charge,
            spend.tokens,
        )
        return BudgetReservation(
            scope=scope,
            charge=charge,
            estimated_input_tokens=estimated,
            output_cap=output_cap,
        )

    def settle(
        self,
        reservation: BudgetReservation,
        usage: object | None,
        *,
        reported: bool = True,
    ) -> UsageCharge:
        spend = self._scope(reservation.scope)
        settled = settle_charge(usage, reserved=reservation.charge, reported=reported)
        spend.tokens = max(0, spend.tokens - reservation.charge + settled.charge)
        self._save()
        log.debug(
            "[Workflow.budget] feature=%s scope=%s outcome=settled measured=%s charge=%s spent=%s",
            self.feature_id,
            reservation.scope,
            settled.measured,
            settled.charge,
            spend.tokens,
        )
        if self.feature_spend() > self.limits.feature_tokens:
            log.warning(
                "[Workflow.budget] feature=%s scope=%s outcome=refused code=%s",
                self.feature_id,
                reservation.scope,
                FEATURE_TOKEN_OVERAGE,
            )
            raise WorkflowBudgetExceeded(FEATURE_TOKEN_OVERAGE)
        return settled

    def record_interrupted(self, reservation: BudgetReservation) -> None:
        """Retain the reservation after an attempt whose outcome is unknown."""

        log.warning(
            "[Workflow.budget] feature=%s scope=%s outcome=unresolved charge=%s",
            self.feature_id,
            reservation.scope,
            reservation.charge,
        )
        self._save()

    # ------------------------------------------------------------------
    # Effect markers
    # ------------------------------------------------------------------
    def begin_effect(self, operation_id: str, scope: str) -> None:
        """Persist an in-flight call marker before dispatch."""

        if self.control.pending_effect is not None:
            raise StateConflict("An effect is already pending for this feature")
        self.control.pending_effect = PendingEffect(
            kind=EffectKind.LLM_CALL,
            operation_id=operation_id,
            task_id=None if scope == PLAN_SCOPE else scope,
            calls=1,
            confirmed=False,
        )
        self._save()
        log.debug(
            "[Workflow.budget] feature=%s scope=%s operation=%s outcome=effect_pending",
            self.feature_id,
            scope,
            operation_id,
        )

    def end_effect(self, operation_id: str) -> None:
        """Clear a marker only after the outcome has been persisted."""

        pending = self.control.pending_effect
        if pending is None:
            return
        if pending.operation_id != operation_id:
            raise StateConflict("Pending effect identity does not match")
        self.control.pending_effect = None
        self._save()
