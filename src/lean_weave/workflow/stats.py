"""Read-only workflow statistics projection.

The projection joins already-durable sources: the canonical attempt journal and
the existing control checkpoint. It never writes, books, retries, or starts a
workflow, and it never returns source text or prompts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from lean_weave.metrics.analytics import AnalyticsReport, aggregate_usage
from lean_weave.metrics.models import ReportingSnapshot
from lean_weave.workflow.results import WorkflowResult
from lean_weave.workflow.state import ControlRecord, StageAttemptObservation


@dataclass
class FeatureStats:
    schema_version: int
    feature_id: str
    status: str = "unavailable"
    reason: str = ""
    stage: str = "unknown"
    archived: bool = False
    available: bool = False
    partial_reason: str | None = None
    workflow_retry_count: int | None = None
    verification_status: str | None = None
    verification_blocking: bool | None = None
    active_ms_total: int | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    stages: list[dict[str, Any]] = field(default_factory=list)
    models: list[dict[str, Any]] = field(default_factory=list)
    strategies: list[dict[str, Any]] = field(default_factory=list)
    cases: list[dict[str, Any]] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _attempt_payload(attempt: StageAttemptObservation) -> dict[str, Any]:
    return {
        "ordinal": attempt.ordinal,
        "stage": attempt.stage,
        "task_id": attempt.task_id,
        "status": attempt.status,
        "duration_ms": attempt.duration_ms,
        "verification_status": attempt.verification_status,
        "verification_blocking": attempt.verification_blocking,
        "context": [item.model_dump(mode="json") for item in attempt.context],
    }


def _context_summary(control: ControlRecord) -> dict[str, Any]:
    contexts = [
        observation
        for attempt in control.closed_attempts
        for observation in attempt.context
    ]
    if not contexts:
        return {"available": False, "gap": "not_observed"}
    files = [item.selected_files for item in contexts if item.selected_files is not None]
    symbols = [item.selected_symbols for item in contexts if item.selected_symbols is not None]
    tokens = [item.pack_input_tokens for item in contexts if item.pack_input_tokens is not None]
    retrievals = [item.retrieval_ms for item in contexts if item.retrieval_ms is not None]
    return {
        "available": True,
        "samples": len(contexts),
        "selected_files_max": max(files) if files else None,
        "selected_symbols_max": max(symbols) if symbols else None,
        "pack_input_tokens_max": max(tokens) if tokens else None,
        "retrieval_ms_total": sum(retrievals) if retrievals else None,
        "cache_hits": sum(item.cache_hits or 0 for item in contexts),
        "cache_misses": sum(item.cache_misses or 0 for item in contexts),
        "cache_observations_available": all(
            item.cache_observations_available for item in contexts
        ),
        "expansion_attempts": sum(item.expansion_attempts or 0 for item in contexts),
        "expansion_successes": sum(item.expansion_successes or 0 for item in contexts),
        "repeated_fragments": sum(item.repeated_fragments or 0 for item in contexts),
        "repeated_bytes": sum(item.repeated_bytes or 0 for item in contexts),
        "refusals": sum(1 for item in contexts if item.refused),
    }


def _task_rows(control: ControlRecord, workflow: WorkflowResult | None) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    if workflow is None:
        return tasks
    for task in workflow.tasks:
        tasks.append(
            {
                "id": task.id,
                "title": task.title,
                "status": task.status.value,
                "tokens": task.tokens,
                "calls": task.calls,
            }
        )
    return tasks


def _rows(report: AnalyticsReport, dimension: str) -> list[dict[str, Any]]:
    rows = []
    for row in report.rows:
        if dimension not in row.dimensions:
            continue
        rows.append(
            {
                "name": row.dimensions.get(dimension),
                "attempts": row.attempts,
                "logical_calls": row.logical_calls,
                "failed_attempts": row.failed_attempts,
                "unresolved_attempts": row.unresolved_attempts,
                "retry_attempts": row.retry_attempts,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "total_tokens": row.total_tokens,
                "cache_hit_tokens": row.cache_hit_tokens,
                "reasoning_tokens": row.reasoning_tokens,
                "budget_charge": row.budget_charge,
                "duration_ms": row.duration_ms,
            }
        )
    return rows


def project_feature_stats(
    *,
    feature_id: str,
    state: Any,
    control: ControlRecord,
    workflow: WorkflowResult | None,
    snapshot: ReportingSnapshot,
    archived: bool = False,
) -> FeatureStats:
    """Join one checkpoint and journal snapshot without cross-file write claims."""

    report = aggregate_usage(snapshot)
    stage_report = aggregate_usage(snapshot, group_by=("stage",))
    model_report = aggregate_usage(snapshot, group_by=("model",))
    strategy_report = aggregate_usage(snapshot, group_by=("strategy",))
    task_report = aggregate_usage(snapshot, group_by=("task",))
    case_report = aggregate_usage(snapshot, group_by=("case",))
    verification = getattr(state, "verification", None)
    verification_status = getattr(getattr(verification, "status", None), "value", None)
    verification_blocking = (
        bool(getattr(verification, "blocking", False)) if verification is not None else None
    )
    closed = [_attempt_payload(item) for item in control.closed_attempts]
    partial_reason = None
    if not snapshot.available:
        partial_reason = snapshot.limit_reason or "journal_unavailable"
    elif control.open_attempt is not None:
        partial_reason = "in_flight_attempt"
    elif control.telemetry_incomplete:
        partial_reason = "telemetry_incomplete"
    if control.truncated_attempts:
        partial_reason = partial_reason or "telemetry_truncated"
    return FeatureStats(
        schema_version=1,
        feature_id=feature_id,
        status=(workflow.status.value if workflow is not None else "unavailable"),
        reason=(workflow.reason if workflow is not None else ""),
        stage=getattr(getattr(state, "stage", None), "value", "unknown"),
        archived=archived,
        available=snapshot.available,
        partial_reason=partial_reason,
        workflow_retry_count=(
            getattr(state, "retry_count", None) if state is not None else None
        ),
        verification_status=verification_status,
        verification_blocking=verification_blocking,
        active_ms_total=control.active_ms_total,
        attempts=closed,
        tasks=_task_rows(control, workflow),
        usage=report.to_dict(),
        stages=_rows(stage_report, "stage"),
        models=_rows(model_report, "model"),
        strategies=_rows(strategy_report, "strategy"),
        cases=_rows(case_report, "case"),
        context=_context_summary(control),
        evidence={
            "gateway": "attempt_journal",
            "workflow": "checkpoint",
            "case_rows": _rows(task_report, "task"),
            "truncated_attempts": control.truncated_attempts,
            "next_stage_attempt": control.next_stage_attempt,
        },
    )
