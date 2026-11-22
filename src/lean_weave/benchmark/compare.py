"""Versioned benchmark comparison and regression-gate eligibility."""

from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from lean_weave.benchmark.models import BENCHMARK_SCHEMA_VERSION

MAX_RESULT_BYTES = 16 * 1024 * 1024
AXES = {"strategy", "map", "budget", "cache", "revision"}
LOWER_IS_BETTER = {
    "missing_dependency_count",
    "selected_relevant_tokens",
    "token_delta",
    "total_tokens",
    "retry_frequency",
    "duration_ms",
}
HIGHER_IS_BETTER = {
    "file_precision",
    "file_recall",
    "symbol_precision",
    "symbol_recall",
    "correctness",
    "success_rate",
}


class ComparisonError(ValueError):
    """Result decoding or comparability failure with a stable code."""


@dataclass
class Gate:
    metric: str
    provenance: str
    scope: str
    max_regression: float


@dataclass
class ComparisonResult:
    schema_version: int
    axis: str
    baseline: str
    candidate: str
    comparable: bool
    reasons: list[str] = field(default_factory=list)
    pairs: int = 0
    baseline_correct: int = 0
    candidate_correct: int = 0
    scheduled: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)
    gates: list[dict[str, Any]] = field(default_factory=list)
    failed_gates: list[str] = field(default_factory=list)
    unevaluable_gates: list[str] = field(default_factory=list)
    baseline_variant: str | None = None
    candidate_variant: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_run(path: Path) -> dict[str, Any]:
    try:
        size = Path(path).stat().st_size
    except OSError as exc:
        raise ComparisonError("result_unreadable") from exc
    if size > MAX_RESULT_BYTES:
        raise ComparisonError("result_too_large")
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ComparisonError("result_malformed") from exc
    if not isinstance(payload, Mapping):
        raise ComparisonError("result_not_object")
    if payload.get("schema_version") != BENCHMARK_SCHEMA_VERSION:
        raise ComparisonError("result_schema_unsupported")
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ComparisonError("result_rows_missing")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("arm_id"), str):
            raise ComparisonError("result_row_invalid")
        if row["arm_id"] in seen:
            raise ComparisonError("result_duplicate_row")
        seen.add(row["arm_id"])
    return dict(payload)


def load_thresholds(path: Path) -> list[Gate]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ComparisonError("thresholds_malformed") from exc
    if not isinstance(payload, Mapping) or payload.get("version") != 1:
        raise ComparisonError("thresholds_schema")
    raw = payload.get("gates")
    if not isinstance(raw, list) or not raw:
        raise ComparisonError("thresholds_gates_missing")
    gates: list[Gate] = []
    seen: set[tuple[str, str, str]] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise ComparisonError("threshold_gate_invalid")
        metric = item.get("metric")
        provenance = item.get("provenance")
        scope = item.get("scope")
        limit = item.get("max_regression")
        if not all(isinstance(value, str) and value for value in (metric, provenance, scope)):
            raise ComparisonError("threshold_gate_invalid")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, (int, float))
            or not (limit >= 0)
        ):
            raise ComparisonError("threshold_limit_invalid")
        key = (metric, provenance, scope)
        if key in seen:
            raise ComparisonError("threshold_duplicate_gate")
        seen.add(key)
        gates.append(Gate(metric=metric, provenance=provenance, scope=scope, max_regression=float(limit)))
    return gates


def _variant_matches(row: Mapping[str, Any], variant: str | None) -> bool:
    return variant is None or row.get("variant") == variant


def _axis_pair(left: Mapping[str, Any], right: Mapping[str, Any], axis: str) -> tuple[bool, str | None]:
    if left.get("status") == "not_started" or right.get("status") == "not_started":
        return True, None
    if left.get("provenance") != right.get("provenance"):
        return False, "provenance_mismatch"
    if left.get("fixture_fingerprint") != right.get("fixture_fingerprint"):
        return False, "fixture_mismatch"
    if axis != "revision" and left.get("origin_head") != right.get("origin_head"):
        return False, "revision_mismatch"
    left_conditions = dict(left.get("conditions") or {})
    right_conditions = dict(right.get("conditions") or {})
    differing = {
        key
        for key in set(left_conditions) | set(right_conditions)
        if left_conditions.get(key) != right_conditions.get(key)
    }
    allowed = {
        "strategy": {"context_mode"},
        "map": {"context.packing"},
        "budget": {"context.packing"},
        "cache": {"context.cache"},
        "revision": set(),
    }[axis]
    if not differing <= allowed:
        return False, "condition_drift"
    return True, None


def _metric_values(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    complete = [
        row
        for row in rows
        if row["candidate"].get("correct") is True
        and row["baseline"].get("correct") is True
        and row["candidate"].get("total_tokens") is not None
        and row["baseline"].get("total_tokens") is not None
        and all((row["candidate"].get("coverage") or {}).values())
        and all((row["baseline"].get("coverage") or {}).values())
    ]
    deltas = []
    for row in complete:
        candidate = row["candidate"]
        baseline = row["baseline"]
        if candidate.get("total_tokens") is None or baseline.get("total_tokens") is None:
            continue
        deltas.append(candidate["total_tokens"] - baseline["total_tokens"])
    return {
        "success_rate": (
            sum(1 for row in rows if row["candidate"].get("correct") is True) / len(rows)
            if rows
            else None
        ),
        "retry_frequency": (
            sum((row["candidate"].get("workflow_retries") or 0) for row in rows) / len(rows)
            if rows
            else None
        ),
        "duration_ms": (
            statistics.median(
                row["candidate"].get("duration_ms") or 0 for row in rows
            )
            if rows
            else None
        ),
        "successful_pairs": len(complete),
        "token_delta_median": (
            statistics.median(deltas) if deltas else None
        ),
        "token_delta_evaluable": bool(deltas),
    }


def compare_runs(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    axis: str,
    baseline_variant: str | None = None,
    candidate_variant: str | None = None,
    thresholds: list[Gate] | None = None,
) -> ComparisonResult:
    if axis not in AXES:
        raise ComparisonError("axis_unsupported")
    baseline_rows = [
        row
        for row in baseline.get("rows", [])
        if row.get("suite") == "workflow"
        and _variant_matches(row, baseline_variant)
    ]
    candidate_rows = [
        row
        for row in candidate.get("rows", [])
        if row.get("suite") == "workflow"
        and _variant_matches(row, candidate_variant)
    ]
    if baseline_variant is None:
        variants = {row.get("variant") for row in baseline_rows}
        if len(variants) > 1:
            raise ComparisonError("baseline_variant_required")
        baseline_variant = next(iter(variants), None)
    if candidate_variant is None:
        variants = {row.get("variant") for row in candidate_rows}
        if len(variants) > 1:
            raise ComparisonError("candidate_variant_required")
        candidate_variant = next(iter(variants), None)
    key = lambda row: (
        row.get("suite"),
        row.get("case_id"),
        row.get("variant"),
        row.get("repetition"),
    )
    baseline_by_key = {key(row): row for row in baseline_rows}
    candidate_by_key = {key(row): row for row in candidate_rows}
    result = ComparisonResult(
        schema_version=1,
        axis=axis,
        baseline=str(baseline.get("revision") or "unknown"),
        candidate=str(candidate.get("revision") or "unknown"),
        comparable=True,
        baseline_variant=baseline_variant,
        candidate_variant=candidate_variant,
    )
    # Pair the declared schedules. A missing key is incompatible, never an
    # inner join that silently drops failed or incomplete arms.
    schedule = sorted(
        {(row.get("suite"), row.get("case_id"), row.get("repetition")) for row in baseline_rows}
    )
    candidate_schedule = {
        (row.get("suite"), row.get("case_id"), row.get("repetition")) for row in candidate_rows
    }
    if set(schedule) != candidate_schedule:
        result.comparable = False
        result.reasons.append("schedule_mismatch")
    else:
        for suite, case_id, repetition in schedule:
            left = baseline_by_key.get((suite, case_id, baseline_variant, repetition))
            right = candidate_by_key.get((suite, case_id, candidate_variant, repetition))
            if left is None or right is None:
                result.comparable = False
                result.reasons.append("arm_missing")
                continue
            eligible, reason = _axis_pair(left, right, axis)
            if not eligible:
                result.comparable = False
                if reason:
                    result.reasons.append(reason)
            result.scheduled += 1
            result.baseline_correct += int(left.get("correct") is True)
            result.candidate_correct += int(right.get("correct") is True)
            result.pairs += 1
            result.metrics.setdefault("pairs", []).append(
                {"case_id": case_id, "repetition": repetition, "baseline": left, "candidate": right}
            )
    result.metrics.update(_metric_values(result.metrics.pop("pairs", [])))
    result.metrics["baseline_correct"] = result.baseline_correct
    result.metrics["candidate_correct"] = result.candidate_correct
    if result.baseline_correct != result.candidate_correct:
        result.reasons.append("correctness_changed")
    if thresholds:
        for gate in thresholds:
            outcome = _evaluate_gate(gate, result.metrics)
            payload = {
                "metric": gate.metric,
                "provenance": gate.provenance,
                "scope": gate.scope,
                "max_regression": gate.max_regression,
                "observed": outcome.get("observed"),
                "status": outcome["status"],
            }
            result.gates.append(payload)
            if outcome["status"] == "failed":
                result.failed_gates.append(gate.metric)
            elif outcome["status"] == "unevaluable":
                result.unevaluable_gates.append(gate.metric)
    return result


def _evaluate_gate(gate: Gate, metrics: Mapping[str, Any]) -> dict[str, Any]:
    value = metrics.get(gate.metric)
    if value is None:
        return {"status": "unevaluable", "observed": None}
    if gate.metric in LOWER_IS_BETTER:
        failed = float(value) > gate.max_regression
    elif gate.metric in HIGHER_IS_BETTER:
        failed = float(value) < gate.max_regression
    else:
        return {"status": "unevaluable", "observed": value}
    return {"status": "failed" if failed else "passed", "observed": value}
