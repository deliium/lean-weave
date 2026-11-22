"""Versioned benchmark metric projection over journal + checkpoint sources.

Developer tooling only. Never invents zeros for missing provider fields and
never double-counts cache/reasoning subsets into processed totals.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from lean_weave.context.packing_tokens import ConservativeByteCounter
from lean_weave.metrics.jsonl import JsonlMetricsCollector
from lean_weave.metrics.models import FeatureUsageSummary
from lean_weave.workflow.results import WorkflowResult, project_result
from lean_weave.workflow.state import ControlRecord, JsonCheckpointStore

log = logging.getLogger("lean_weave.benchmarks.results")

RESULTS_SCHEMA_VERSION = 1
EVIDENCE_SCHEMA_VERSION = 1
Provenance = Literal["mock_functional", "local_estimate", "deepseek_live", "unavailable"]
DispatchEvidence = Literal["no_send", "observed", "unknown"]
ATTEMPT_STATUSES = frozenset(
    {"settled", "failed", "unresolved", "cancelled", "open", "unknown"}
)


@dataclass
class FieldCoverage:
    input_tokens: bool = False
    output_tokens: bool = False
    total_tokens: bool = False
    cache_hit_tokens: bool = False
    reasoning_tokens: bool = False
    estimated_input_tokens: bool = False

    @property
    def any_reported(self) -> bool:
        return any(
            (
                self.input_tokens,
                self.output_tokens,
                self.total_tokens,
                self.cache_hit_tokens,
                self.reasoning_tokens,
            )
        )


@dataclass
class TokenRollup:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_processed_tokens: int | None = None
    cache_hit_tokens: int | None = None
    reasoning_tokens: int | None = None
    estimated_input_tokens: int | None = None
    budget_charge_tokens: int | None = None
    coverage: FieldCoverage = field(default_factory=FieldCoverage)


@dataclass
class AttemptProjection:
    reservation_id: str | None
    stage: str
    status: str
    is_gateway_retry: bool
    logical_call: bool
    tokens: TokenRollup
    charge_tokens: int
    measured: bool


@dataclass
class StageProjection:
    stage: str
    attempts: int
    logical_calls: int
    failed_attempts: int
    unresolved_attempts: int
    tokens: TokenRollup
    # Workflow stage name "retry" is repair, not transport retry.
    workflow_repair_stage: bool
    estimated_input_tokens: int | None = None


@dataclass
class ContextSizeProjection:
    """Initial stage bundle metrics (not full prepared-request estimates)."""

    estimated_tokens: int | None = None
    admitted_excerpt_chars: int | None = None
    unit: str = "context_pack_estimated_tokens"
    available: bool = False
    gap: str | None = "not_observed_in_checkpoint"


@dataclass
class CheckHistoryProjection:
    executions: int | None = None
    passes: int | None = None
    failures: int | None = None
    available: bool = False
    gap: str | None = "final_verification_is_one_batch_not_history"


@dataclass
class BenchmarkMetricReport:
    schema_version: int
    feature_id: str
    provenance: Provenance
    workflow_status: str | None
    workflow_reason: str | None
    workflow_retry_count: int | None
    logical_workflow_calls: int
    journal_reservations: int
    journal_confirmed_outcomes: int
    journal_uncertain_attempts: int
    gateway_retry_attempts: int
    workflow_repair_count: int
    verification_llm_sends: int
    tokens: TokenRollup
    stages: list[StageProjection]
    attempts: list[AttemptProjection]
    context_size: ContextSizeProjection
    checks: CheckHistoryProjection
    inconsistencies: list[str] = field(default_factory=list)
    context_mode: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _optional_int(value: int | None, *, present: bool) -> int | None:
    if not present:
        return None
    return int(value or 0)


def _token_rollup_from_summary(summary: FeatureUsageSummary) -> TokenRollup:
    presence = summary.presence
    counters = summary.counters
    estimated_inputs = [
        item.estimated_input_tokens
        for item in summary.attempts
        if item.estimated_input_tokens is not None
    ]
    coverage = FieldCoverage(
        input_tokens=presence.input_tokens,
        output_tokens=presence.output_tokens,
        total_tokens=presence.total_tokens,
        cache_hit_tokens=presence.cache_hit_tokens,
        reasoning_tokens=presence.reasoning_tokens,
        estimated_input_tokens=bool(estimated_inputs),
    )
    return TokenRollup(
        prompt_tokens=_optional_int(counters.input_tokens, present=presence.input_tokens),
        completion_tokens=_optional_int(
            counters.output_tokens, present=presence.output_tokens
        ),
        total_processed_tokens=_optional_int(
            counters.total_tokens, present=presence.total_tokens
        ),
        cache_hit_tokens=_optional_int(
            counters.cache_hit_tokens, present=presence.cache_hit_tokens
        ),
        reasoning_tokens=_optional_int(
            counters.reasoning_tokens, present=presence.reasoning_tokens
        ),
        estimated_input_tokens=(
            sum(estimated_inputs) if estimated_inputs else None
        ),
        budget_charge_tokens=summary.budget_charge,
        coverage=coverage,
    )


def project_metrics(
    *,
    feature_id: str,
    journal: JsonlMetricsCollector,
    store: JsonCheckpointStore | None = None,
    provenance: Provenance = "mock_functional",
    context_estimated_tokens: int | None = None,
    context_admitted_chars: int | None = None,
    check_executions: int | None = None,
    check_passes: int | None = None,
    check_failures: int | None = None,
    context_mode: str | None = None,
) -> BenchmarkMetricReport:
    """Project a bounded metric report from reopened journal + checkpoint."""

    log.debug("[Benchmark.metrics] feature=%s provenance=%s", feature_id, provenance)
    summary = journal.summary(feature_id)
    inconsistencies: list[str] = []
    workflow_status = None
    workflow_reason = None
    workflow_retry_count = None
    workflow: WorkflowResult | None = None
    if store is not None:
        try:
            envelope = store.load_checkpoint(feature_id)
            workflow = project_result(envelope.state, envelope.control)
            workflow_status = workflow.status.value
            workflow_reason = workflow.reason
            workflow_retry_count = envelope.state.retry_count
            checkpoint_calls = envelope.state.metrics.llm_calls
            if checkpoint_calls != summary.attempts_total - summary.unresolved_attempts:
                # Soft signal only: unresolved reservations are not confirmed sends.
                if checkpoint_calls > summary.attempts_total:
                    inconsistencies.append("checkpoint_calls_exceed_journal_attempts")
        except Exception as exc:  # noqa: BLE001 - projection stays available
            inconsistencies.append(f"checkpoint_unavailable:{type(exc).__name__}")
            log.warning(
                "[Benchmark.metrics] feature=%s outcome=checkpoint_gap",
                feature_id,
            )

    attempts: list[AttemptProjection] = []
    confirmed = 0
    uncertain = 0
    gateway_retries = 0
    for item in summary.attempts:
        if item.status == "unresolved":
            uncertain += 1
            logical = False
        else:
            confirmed += 1
            logical = True
        if item.attempt_ordinal > 0:
            gateway_retries += 1
        coverage = FieldCoverage(
            input_tokens=item.counters.input_tokens is not None,
            output_tokens=item.counters.output_tokens is not None,
            total_tokens=item.counters.total_tokens is not None,
            cache_hit_tokens=item.counters.cache_hit_tokens is not None,
            reasoning_tokens=item.counters.reasoning_tokens is not None,
            estimated_input_tokens=item.estimated_input_tokens is not None,
        )
        attempts.append(
            AttemptProjection(
                reservation_id=item.reservation_id,
                stage=item.stage,
                status=item.status,
                is_gateway_retry=item.attempt_ordinal > 0,
                logical_call=logical,
                tokens=TokenRollup(
                    prompt_tokens=item.counters.input_tokens,
                    completion_tokens=item.counters.output_tokens,
                    total_processed_tokens=item.counters.total_tokens,
                    cache_hit_tokens=item.counters.cache_hit_tokens,
                    reasoning_tokens=item.counters.reasoning_tokens,
                    estimated_input_tokens=item.estimated_input_tokens,
                    budget_charge_tokens=item.charge_tokens,
                    coverage=coverage,
                ),
                charge_tokens=item.charge_tokens,
                measured=item.measured,
            )
        )

    stages: list[StageProjection] = []
    repair_stage_attempts = 0
    for stage in summary.stages:
        unresolved = sum(
            1
            for item in summary.attempts
            if item.stage == stage.stage and item.status == "unresolved"
        )
        if stage.stage == "retry":
            repair_stage_attempts += stage.attempts
        presence = stage.presence
        stage_estimated = [
            item.estimated_input_tokens
            for item in summary.attempts
            if item.stage == stage.stage and item.estimated_input_tokens is not None
        ]
        stages.append(
            StageProjection(
                stage=stage.stage,
                attempts=stage.attempts,
                logical_calls=stage.attempts - unresolved,
                failed_attempts=stage.failed_attempts,
                unresolved_attempts=unresolved,
                tokens=TokenRollup(
                    prompt_tokens=_optional_int(
                        stage.measured.input_tokens, present=presence.input_tokens
                    ),
                    completion_tokens=_optional_int(
                        stage.measured.output_tokens, present=presence.output_tokens
                    ),
                    total_processed_tokens=_optional_int(
                        stage.measured.total_tokens, present=presence.total_tokens
                    ),
                    cache_hit_tokens=_optional_int(
                        stage.measured.cache_hit_tokens,
                        present=presence.cache_hit_tokens,
                    ),
                    reasoning_tokens=_optional_int(
                        stage.measured.reasoning_tokens,
                        present=presence.reasoning_tokens,
                    ),
                    budget_charge_tokens=stage.measured_charge + stage.estimated_charge,
                    coverage=FieldCoverage(
                        input_tokens=presence.input_tokens,
                        output_tokens=presence.output_tokens,
                        total_tokens=presence.total_tokens,
                        cache_hit_tokens=presence.cache_hit_tokens,
                        reasoning_tokens=presence.reasoning_tokens,
                    ),
                ),
                workflow_repair_stage=stage.stage == "retry",
                estimated_input_tokens=(
                    sum(stage_estimated) if stage_estimated else None
                ),
            )
        )

    context = ContextSizeProjection(
        estimated_tokens=context_estimated_tokens,
        admitted_excerpt_chars=context_admitted_chars,
        available=context_estimated_tokens is not None,
        gap=None if context_estimated_tokens is not None else "not_observed_in_checkpoint",
    )
    checks = CheckHistoryProjection(
        executions=check_executions,
        passes=check_passes,
        failures=check_failures,
        available=check_executions is not None,
        gap=None if check_executions is not None else "final_verification_is_one_batch_not_history",
    )

    logical_calls = confirmed
    verification_llm_sends = sum(
        1 for item in attempts if item.stage == "verify" and item.logical_call
    )
    report = BenchmarkMetricReport(
        schema_version=RESULTS_SCHEMA_VERSION,
        feature_id=feature_id,
        provenance=provenance,
        workflow_status=workflow_status,
        workflow_reason=workflow_reason,
        workflow_retry_count=workflow_retry_count,
        logical_workflow_calls=logical_calls,
        journal_reservations=len(summary.attempts),
        journal_confirmed_outcomes=confirmed,
        journal_uncertain_attempts=uncertain,
        gateway_retry_attempts=gateway_retries,
        workflow_repair_count=workflow_retry_count if workflow_retry_count is not None else repair_stage_attempts,
        verification_llm_sends=verification_llm_sends,
        tokens=_token_rollup_from_summary(summary),
        stages=stages,
        attempts=attempts,
        context_size=context,
        checks=checks,
        inconsistencies=inconsistencies,
        context_mode=context_mode,
    )
    log.debug(
        "[Benchmark.metrics] feature=%s reservations=%s confirmed=%s uncertain=%s",
        feature_id,
        report.journal_reservations,
        report.journal_confirmed_outcomes,
        report.journal_uncertain_attempts,
    )
    if inconsistencies:
        log.warning(
            "[Benchmark.metrics] feature=%s inconsistencies=%s",
            feature_id,
            len(inconsistencies),
        )
    return report


COMPONENT_SCHEMA_VERSION = 1
_TAG_COMPONENTS = {
    "task_specification": "task_instructions",
    "repository_map": "repository_map",
    "source_fragments": "approved_source",
    "editable_revisions": "editable_revisions",
}
_TAG_PATTERN = re.compile(r"<([a-z_]+)>(.*?)</\1>", re.DOTALL)


def _component_estimator() -> ConservativeByteCounter:
    return ConservativeByteCounter()


def _request_components(request: Mapping[str, Any]) -> dict[str, int]:
    """Estimate transient request components with the packing estimator."""

    counter = _component_estimator()
    components = {
        "system_instructions": 0,
        "task_instructions": 0,
        "repository_map": 0,
        "approved_source": 0,
        "editable_revisions": 0,
        "other_user": 0,
        "tool_messages": 0,
        "tool_schemas": 0,
        "response_format": 0,
    }
    for message in request.get("messages") or ():
        if not isinstance(message, Mapping):
            continue
        role = str(message.get("role") or "")
        content = message.get("content") or ""
        if role == "system":
            components["system_instructions"] += counter.count_text(str(content))
        elif role == "user":
            text = str(content)
            matched = False
            for tag, body in _TAG_PATTERN.findall(text):
                key = _TAG_COMPONENTS.get(tag)
                if key is not None:
                    components[key] += counter.count_text(body)
                    matched = True
            if not matched:
                components["other_user"] += counter.count_text(text)
        elif role in {"assistant", "tool"}:
            components["tool_messages"] += counter.count_text(str(content))
    tools = request.get("tools")
    if tools:
        components["tool_schemas"] += counter.count_text(
            json.dumps(tools, sort_keys=True, separators=(",", ":"))
        )
    response_format = request.get("response_format")
    if response_format:
        components["response_format"] += counter.count_text(
            json.dumps(response_format, sort_keys=True, separators=(",", ":"))
        )
    return components


def project_request_components(
    *,
    requests: Sequence[Mapping[str, Any]],
    attempts: Sequence[Any] = (),
) -> dict[str, Any]:
    """Project explanatory input components for sent requests only.

    Components use the same conservative estimator as packing, but they are
    explanatory slices of transient requests and are not assumed to sum to a
    complete request estimate. Only counts and digests are retained.
    """

    counter = _component_estimator()
    totals = {
        key: 0
        for key in (
            "system_instructions",
            "task_instructions",
            "repository_map",
            "approved_source",
            "editable_revisions",
            "other_user",
            "tool_messages",
            "tool_schemas",
            "response_format",
        )
    }
    by_stage: dict[str, dict[str, int]] = {}
    rows: list[dict[str, Any]] = []
    for index, request in enumerate(requests):
        components = _request_components(request)
        for key, value in components.items():
            totals[key] += value
        stage = (
            str(getattr(attempts[index], "stage", "unknown"))
            if index < len(attempts)
            else "unknown"
        )
        stage_totals = by_stage.setdefault(stage, dict.fromkeys(totals, 0))
        for key, value in components.items():
            stage_totals[key] += value
        encoded = json.dumps(
            request, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
        rows.append(
            {
                "stage": stage,
                "request_digest": "sha256:" + hashlib.sha256(encoded).hexdigest(),
                "components": components,
                "request_estimate": counter.count_request(request),
            }
        )
    return {
        "schema_version": COMPONENT_SCHEMA_VERSION,
        "estimator": counter.name,
        "sent_requests": len(requests),
        "unsent_reservations": max(0, len(attempts) - len(requests)),
        "components": totals,
        "by_stage": {
            stage: dict(sorted(values.items())) for stage, values in sorted(by_stage.items())
        },
        "requests": rows,
        "note": "component estimates are explanatory and need not sum to the request estimate",
    }


def aggregate_component_evidence(rows: Sequence[Any]) -> dict[str, Any]:
    """Aggregate per-arm component evidence without retaining request text."""

    components: dict[str, int] = {}
    by_variant: dict[str, dict[str, int]] = {}
    arms = 0
    sent = 0
    unsent = 0
    for row in rows:
        evidence = getattr(row, "evidence", None) or {}
        item = evidence.get("input_components")
        if not item:
            continue
        arms += 1
        sent += int(item.get("sent_requests") or 0)
        unsent += int(item.get("unsent_reservations") or 0)
        variant = str(getattr(row, "variant", "unknown"))
        bucket = by_variant.setdefault(variant, {})
        for key, value in (item.get("components") or {}).items():
            components[key] = components.get(key, 0) + int(value)
            bucket[key] = bucket.get(key, 0) + int(value)
    return {
        "schema_version": COMPONENT_SCHEMA_VERSION,
        "arms": arms,
        "sent_requests": sent,
        "unsent_reservations": unsent,
        "components": dict(sorted(components.items())),
        "by_variant": {
            name: dict(sorted(values.items())) for name, values in sorted(by_variant.items())
        },
        "estimator": "conservative_estimate",
    }


def project_attempt_evidence(
    *,
    snapshot: ReportingSnapshot,
    control: ControlRecord | None = None,
    transport_sends: int | None = None,
) -> dict[str, Any]:
    """Project bounded attempt/stage evidence for benchmark results.

    Developer tooling only: uses the existing journal + checkpoint sources and
    never persists source text, prompts, tool payloads or raw journal bytes.
    ``transport_sends`` is independent offline-transport evidence: zero sends
    with no journal is a proven no-send, not an absent-journal guess.
    """

    attempts: list[dict[str, Any]] = []
    unresolved = 0
    for item in snapshot.attempts:
        status = _enum(item.status, ATTEMPT_STATUSES, "unknown")
        if status == "unresolved":
            unresolved += 1
        attempts.append(
            {
                "reservation_id": _bounded(item.reservation_id, 64),
                "stage": _bounded(item.stage, 32),
                "task_id": _bounded(item.task_id, 64),
                "call_ordinal": int(item.attempt_ordinal),
                "transport_retry": item.attempt_ordinal > 0,
                "status": status,
                "error_code": _bounded(item.error_code, 64),
                "finish_reason": _bounded(item.finish_reason, 64),
                "input_tokens": item.counters.input_tokens,
                "output_tokens": item.counters.output_tokens,
                "total_tokens": item.counters.total_tokens,
                "cache_hit_tokens": item.counters.cache_hit_tokens,
                "reasoning_tokens": item.counters.reasoning_tokens,
                "estimated_input_tokens": item.estimated_input_tokens,
                "charge_tokens": item.charge_tokens,
                "latency_ms": item.duration_ms,
                "measured": item.measured,
                "legacy": item.legacy,
            }
        )
    if not snapshot.available:
        dispatch: DispatchEvidence = "unknown"
    elif not snapshot.attempts:
        dispatch = "no_send"
    elif unresolved:
        dispatch = "unknown"
    else:
        dispatch = "observed"
    if transport_sends is not None:
        if transport_sends == 0 and not snapshot.attempts:
            dispatch = "no_send"
        elif transport_sends == 0 and snapshot.attempts:
            dispatch = "unknown"
        elif transport_sends > 0 and not snapshot.available:
            dispatch = "unknown"
        elif transport_sends > 0 and not snapshot.attempts:
            dispatch = "unknown"
        elif transport_sends > 0 and unresolved:
            dispatch = "unknown"
        elif transport_sends > 0:
            dispatch = "observed"

    closed: list[dict[str, Any]] = []
    context_observations: list[dict[str, Any]] = []
    if control is not None:
        for observation in control.closed_attempts:
            contexts = [
                {
                    "mode": _bounded(item.mode, 32),
                    "outcome": _bounded(item.outcome, 32),
                    "selected_files": item.selected_files,
                    "selected_symbols": item.selected_symbols,
                    "map_rows": item.map_rows,
                    "pack_input_tokens": item.pack_input_tokens,
                    "retrieval_ms": item.retrieval_ms,
                    "cache_hits": item.cache_hits,
                    "cache_misses": item.cache_misses,
                    "cache_observations_available": item.cache_observations_available,
                    "expansion_attempts": item.expansion_attempts,
                    "expansion_successes": item.expansion_successes,
                    "repeated_fragments": item.repeated_fragments,
                    "repeated_bytes": item.repeated_bytes,
                    "refused": item.refused,
                    "refusal_category": _bounded(item.refusal_category, 64),
                }
                for item in observation.context
            ]
            context_observations.extend(contexts)
            closed.append(
                {
                    "ordinal": int(observation.ordinal),
                    "stage": _bounded(observation.stage, 16),
                    "task_id": _bounded(observation.task_id, 64),
                    "status": _bounded(observation.status, 32),
                    "duration_ms": int(observation.duration_ms),
                    "verification_status": _bounded(
                        observation.verification_status, 32
                    ),
                    "verification_blocking": observation.verification_blocking,
                    "contexts": contexts,
                }
            )
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "dispatch": dispatch,
        "journal_available": snapshot.available,
        "transport_sends": transport_sends,
        "reservations": len(snapshot.attempts),
        "unresolved": unresolved,
        "logical_calls": len(snapshot.attempts) - unresolved,
        "verification_sends": sum(1 for item in attempts if item["stage"] == "verify"),
        "checks_observed": sum(
            1 for item in closed if item["verification_status"] is not None
        ),
        "attempts": attempts,
        "closed_attempts": closed,
        "context_observations": context_observations,
        "active_ms_total": control.active_ms_total if control is not None else None,
        "telemetry_incomplete": bool(
            control.telemetry_incomplete if control is not None else False
        ),
        "truncated_attempts": int(
            control.truncated_attempts if control is not None else 0
        ),
        "limit_reason": _bounded(snapshot.limit_reason, 64),
    }


def _enum(value: str, allowed: frozenset[str], fallback: str) -> str:
    text = str(value or "")
    return text if text in allowed else fallback


def _bounded(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:limit]


def render_human_summary(report: BenchmarkMetricReport) -> str:
    lines = [
        f"Benchmark metrics v{report.schema_version} feature={report.feature_id}",
        f"provenance={report.provenance}",
        f"status={report.workflow_status or 'unavailable'} reason={report.workflow_reason or 'unavailable'}",
        f"logical_calls={report.logical_workflow_calls} reservations={report.journal_reservations}",
        f"confirmed={report.journal_confirmed_outcomes} uncertain={report.journal_uncertain_attempts}",
        f"gateway_retries={report.gateway_retry_attempts} workflow_repairs={report.workflow_repair_count}",
        f"verification_llm_sends={report.verification_llm_sends}",
    ]
    tokens = report.tokens
    lines.append(
        "tokens="
        f"prompt:{_fmt(tokens.prompt_tokens)},"
        f"completion:{_fmt(tokens.completion_tokens)},"
        f"total:{_fmt(tokens.total_processed_tokens)},"
        f"cache:{_fmt(tokens.cache_hit_tokens)},"
        f"reasoning:{_fmt(tokens.reasoning_tokens)},"
        f"budget_charge:{_fmt(tokens.budget_charge_tokens)}"
    )
    if report.context_size.available:
        lines.append(
            f"context_size={report.context_size.estimated_tokens} ({report.context_size.unit})"
        )
    else:
        lines.append(f"context_size=unavailable ({report.context_size.gap})")
    if report.checks.available:
        lines.append(
            f"checks={report.checks.passes}/{report.checks.executions} passed"
        )
    else:
        lines.append(f"checks=unavailable ({report.checks.gap})")
    for stage in report.stages:
        lines.append(
            f"stage={stage.stage} attempts={stage.attempts} logical={stage.logical_calls}"
        )
    for item in report.inconsistencies:
        lines.append(f"inconsistency={item}")
    return "\n".join(lines) + "\n"


def _fmt(value: int | None) -> str:
    return "unavailable" if value is None else str(value)


def write_report(path: Path, report: BenchmarkMetricReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    log.info("[Benchmark.metrics] wrote path=%s feature=%s", path.name, report.feature_id)


def load_report(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("invalid_report")
    return dict(payload)


@dataclass
class WorkflowContextStageComparison:
    stage: str
    v1_estimated_input_tokens: int | None
    v2_estimated_input_tokens: int | None
    delta: int | None


@dataclass
class WorkflowContextComparison:
    """Matched-mode evidence; local estimates are not live API spend."""

    schema_version: int
    case_id: str
    v1_status: str | None
    v2_status: str | None
    v1_sends: int
    v2_sends: int
    v1_estimated_input_tokens: int | None
    v2_estimated_input_tokens: int | None
    stages: list[WorkflowContextStageComparison] = field(default_factory=list)
    strict_reduction: bool = False
    comparable: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compare_workflow_context(
    *,
    case_id: str,
    v1: BenchmarkMetricReport,
    v2: BenchmarkMetricReport,
) -> WorkflowContextComparison:
    """Compare matched successful runs without reinterpreting V1 context size."""

    comparable = (
        v1.workflow_status == "success"
        and v2.workflow_status == "success"
        and v1.provenance == v2.provenance
        and v1.journal_uncertain_attempts == 0
        and v2.journal_uncertain_attempts == 0
    )
    v1_by_stage = {stage.stage: stage for stage in v1.stages}
    v2_by_stage = {stage.stage: stage for stage in v2.stages}
    stages: list[WorkflowContextStageComparison] = []
    for name in sorted(set(v1_by_stage) | set(v2_by_stage)):
        left = v1_by_stage.get(name)
        right = v2_by_stage.get(name)
        left_value = None if left is None else left.estimated_input_tokens
        right_value = None if right is None else right.estimated_input_tokens
        delta = (
            None
            if left_value is None or right_value is None
            else right_value - left_value
        )
        stages.append(
            WorkflowContextStageComparison(
                stage=name,
                v1_estimated_input_tokens=left_value,
                v2_estimated_input_tokens=right_value,
                delta=delta,
            )
        )
    left_total = v1.tokens.estimated_input_tokens
    right_total = v2.tokens.estimated_input_tokens
    strict = bool(
        comparable
        and left_total is not None
        and right_total is not None
        and right_total < left_total
    )
    notes: list[str] = []
    if v1.context_mode is None:
        notes.append("v1_context_mode_unlabelled")
    if v2.context_mode is None:
        notes.append("v2_context_mode_unlabelled")
    if not comparable:
        notes.append("runs_not_matched_and_successful")
    if left_total is None or right_total is None:
        notes.append("estimated_input_missing")
    return WorkflowContextComparison(
        schema_version=1,
        case_id=case_id,
        v1_status=v1.workflow_status,
        v2_status=v2.workflow_status,
        v1_sends=v1.logical_workflow_calls,
        v2_sends=v2.logical_workflow_calls,
        v1_estimated_input_tokens=left_total,
        v2_estimated_input_tokens=right_total,
        stages=stages,
        strict_reduction=strict,
        comparable=comparable,
        notes=notes,
    )
