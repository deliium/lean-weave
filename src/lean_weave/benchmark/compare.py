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
    "selected_source_tokens",
    "token_delta",
    "token_delta_median",
    "total_tokens",
    "total_tokens_delta_median",
    "input_tokens_delta_median",
    "output_tokens_delta_median",
    "estimated_input_tokens_delta_median",
    "logical_calls_delta_median",
    "retry_delta_median",
    "expansion_delta_median",
    "retry_frequency",
    "duration_ms",
    "duration_delta_median",
    "pack_input_tokens",
    "incomplete_pairs",
    "incorrect_pairs",
    "errors",
    "not_started",
}
HIGHER_IS_BETTER = {
    "file_precision",
    "file_recall",
    "symbol_precision",
    "symbol_recall",
    "relevant_token_ratio",
    "correctness",
    "success_rate",
    "success_rate_delta",
}
HIGHER_IS_BETTER_DELTA_PREFIXES = (
    "file_precision",
    "file_recall",
    "symbol_precision",
    "symbol_recall",
    "relevant_token_ratio",
)
LOWER_IS_BETTER_DELTA_PREFIXES = (
    "missing_dependency_count",
    "selected_source_tokens",
)
# Condition leaves that carry benchmark selection metadata rather than the
# effective execution contract. They may differ on every named axis.
IGNORED_LEAVES = {"variant", "digest", "config.digest"}
# Derived from context_extra/config.context; the controlled axis owns them.
AXIS_OWNED_PREFIXES = {"strategy": ("context", "config.context", "strategy")}
RETRIEVAL_SOURCE_REVISION_KEY = "source_revision"


class ComparisonError(ValueError):
    """Result decoding or comparability failure with a stable code."""


@dataclass
class Gate:
    metric: str
    provenance: str
    scope: str
    max_regression: float
    direction: str | None = None
    policy_version: int = 1


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
    if not isinstance(payload, Mapping) or payload.get("version") not in {1, 2}:
        raise ComparisonError("thresholds_schema")
    version = int(payload["version"])
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
        direction = item.get("direction")
        if direction is not None and direction not in {"lower", "higher"}:
            raise ComparisonError("threshold_direction_invalid")
        if scope != "suite" and not scope.startswith("case:"):
            raise ComparisonError("threshold_scope_invalid")
        if scope.startswith("case:") and not scope.split(":", 1)[1].strip():
            raise ComparisonError("threshold_scope_invalid")
        key = (metric, provenance, scope)
        if key in seen:
            raise ComparisonError("threshold_duplicate_gate")
        seen.add(key)
        gates.append(
            Gate(
                metric=metric,
                provenance=provenance,
                scope=scope,
                max_regression=float(limit),
                direction=direction,
                policy_version=version,
            )
        )
    return gates


def _variant_matches(row: Mapping[str, Any], variant: str | None) -> bool:
    return variant is None or row.get("variant") == variant


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, Mapping):
        leaves: dict[str, Any] = {}
        for key, item in value.items():
            nested = f"{prefix}.{key}" if prefix else str(key)
            leaves.update(_flatten(item, nested))
        return leaves
    return {prefix: value}


def _axis_leaf_allowed(axis: str, leaf: str) -> bool:
    if axis == "strategy":
        return (
            leaf in {
                "context_mode",
                "context_extra",
                "context_digest",
                "config.context_digest",
                "strategy",
            }
            or leaf.startswith(
                (
                    "context.",
                    "context_extra.",
                    "config.context.",
                    "strategy.",
                )
            )
        )
    if axis == "map":
        return (
            "map" in leaf
            or "graph" in leaf
            or leaf.startswith(("context.packing.map", "config.context.packing.map"))
        )
    if axis == "budget":
        return leaf.startswith(
            (
                "context.packing.",
                "context_extra.packing.",
                "config.context.packing.",
            )
        ) or leaf in {"source_tokens"}
    if axis == "cache":
        return "cache" in leaf
    return False


def _axis_pair(
    left: Mapping[str, Any], right: Mapping[str, Any], axis: str
) -> tuple[bool, str | None]:
    if left.get("status") == "not_started" or right.get("status") == "not_started":
        return True, None
    if left.get("provenance") != right.get("provenance"):
        return False, "provenance_mismatch"
    if left.get("fixture_fingerprint") != right.get("fixture_fingerprint"):
        return False, "fixture_mismatch"
    # Fixture HEAD is not a free variable even on the implementation-revision
    # axis; only the package source revision may differ there.
    if (left.get("origin_head") or "") != (right.get("origin_head") or ""):
        return False, "fixture_revision_mismatch"
    left_leaves = _flatten(left.get("conditions") or {})
    right_leaves = _flatten(right.get("conditions") or {})
    differing = {
        key
        for key in set(left_leaves) | set(right_leaves)
        if left_leaves.get(key) != right_leaves.get(key) and key not in IGNORED_LEAVES
    }
    disallowed = {key for key in differing if not _axis_leaf_allowed(axis, key)}
    if disallowed:
        return False, "condition_drift"
    return True, None


def _covered(row: Mapping[str, Any], metric: str) -> bool:
    coverage = row.get("coverage") or {}
    if metric in coverage:
        return bool(coverage.get(metric))
    return bool(coverage.get("total_tokens"))


def _signed_delta_series(
    pairs: list[dict[str, Any]], field: str, coverage_field: str
) -> tuple[list[int | float], int]:
    """Candidate-minus-baseline deltas over covered, correct pairs."""

    values: list[int | float] = []
    for row in pairs:
        left = row["baseline"]
        right = row["candidate"]
        if left.get("correct") is not True or right.get("correct") is not True:
            continue
        if not _covered(left, coverage_field) or not _covered(right, coverage_field):
            continue
        left_value = left.get(field)
        right_value = right.get(field)
        if left_value is None or right_value is None:
            continue
        values.append(right_value - left_value)
    return values, len(values)


def _case_deltas(
    pairs: list[dict[str, Any]], field: str, coverage_field: str
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in pairs:
        grouped.setdefault(str(row["case_id"]), []).append(row)
    result: dict[str, dict[str, Any]] = {}
    for case_id, rows in grouped.items():
        values, covered = _signed_delta_series(rows, field, coverage_field)
        result[case_id] = {
            "candidate_minus_baseline": (
                statistics.median(values) if values else None
            ),
            "pairs": len(rows),
            "covered_pairs": covered,
        }
    return result


def _is_higher_better_metric(name: str) -> bool:
    return name.startswith(HIGHER_IS_BETTER_DELTA_PREFIXES)


def _retrieval_metrics(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate typed retrieval payloads without crossing split/K labels.

    Per-case coverage is retained. Suite gates can use the worst per-case delta
    or the aggregate total delta instead of a median that can hide a regression.
    """

    grouped: dict[str, list[float]] = {}
    per_case: dict[str, dict[str, list[float]]] = {}
    sums: dict[str, list[float]] = {}
    for row in pairs:
        case_id = str(row["case_id"])
        left = row["baseline"].get("retrieval") or {}
        right = row["candidate"].get("retrieval") or {}
        if not right:
            continue
        split = str(right.get("split"))
        for family, prefix in (("files", "file"), ("symbols", "symbol")):
            left_metrics = (left.get("ranking") or {}).get(family) or {}
            right_metrics = (right.get("ranking") or {}).get(family) or {}
            if not isinstance(right_metrics, Mapping):
                continue
            for key, value in right_metrics.items():
                if "@" not in key or not isinstance(value, (int, float)):
                    continue
                kind, _, k = key.partition("@")
                name = (
                    f"{prefix}_{'precision' if kind == 'P' else 'recall'}"
                    f"_at_{k}_{split}"
                )
                grouped.setdefault(name, []).append(float(value))
                left_value = left_metrics.get(key) if isinstance(left_metrics, Mapping) else None
                if isinstance(left_value, (int, float)):
                    per_case.setdefault(name, {}).setdefault(case_id, []).append(
                        float(value) - float(left_value)
                    )
                    bucket = sums.setdefault(name, [0.0, 0.0])
                    bucket[0] += float(left_value)
                    bucket[1] += float(value)
        for field in (
            "selected_source_tokens",
            "missing_dependency_count",
            "relevant_token_ratio",
        ):
            right_pack = right.get("pack") or {}
            left_pack = left.get("pack") or {}
            value = right_pack.get(field)
            if not isinstance(value, (int, float)):
                continue
            name = f"{field}_{split}"
            grouped.setdefault(name, []).append(float(value))
            left_value = left_pack.get(field)
            if isinstance(left_value, (int, float)):
                per_case.setdefault(name, {}).setdefault(case_id, []).append(
                    float(value) - float(left_value)
                )
                bucket = sums.setdefault(name, [0.0, 0.0])
                bucket[0] += float(left_value)
                bucket[1] += float(value)
    result: dict[str, Any] = {
        name: round(statistics.median(values), 4)
        for name, values in sorted(grouped.items())
    }
    for name, cases in sorted(per_case.items()):
        case_deltas = {
            case_id: statistics.median(values)
            for case_id, values in cases.items()
            if values
        }
        if not case_deltas:
            continue
        values = list(case_deltas.values())
        result[f"{name}_delta"] = round(statistics.median(values), 4)
        if _is_higher_better_metric(name):
            result[f"{name}_worst_delta"] = round(min(values), 4)
        else:
            result[f"{name}_worst_delta"] = round(max(values), 4)
        bucket = sums.get(name)
        if bucket is not None:
            result[f"{name}_total_delta"] = round(bucket[1] - bucket[0], 4)
    return result


def _metric_values(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    token_deltas, token_pairs = _signed_delta_series(pairs, "total_tokens", "total_tokens")
    input_deltas, _ = _signed_delta_series(pairs, "input_tokens", "input_tokens")
    output_deltas, _ = _signed_delta_series(pairs, "output_tokens", "output_tokens")
    estimated_deltas, _ = _signed_delta_series(
        pairs, "estimated_input_tokens", "estimated_input_tokens"
    )
    call_deltas, _ = _signed_delta_series(pairs, "logical_calls", "total_tokens")
    retry_deltas, _ = _signed_delta_series(pairs, "workflow_retries", "total_tokens")
    duration_deltas, _ = _signed_delta_series(pairs, "duration_ms", "total_tokens")
    expansion_deltas, _ = _signed_delta_series(
        pairs, "expansion_attempts", "total_tokens"
    )

    def median(values: list[int | float]) -> float | None:
        return statistics.median(values) if values else None

    covered_pairs = sum(
        1
        for row in pairs
        if row["baseline"].get("correct") is True
        and row["candidate"].get("correct") is True
        and _covered(row["baseline"], "total_tokens")
        and _covered(row["candidate"], "total_tokens")
    )
    positive = [
        row
        for row in pairs
        if row["baseline"].get("expected_negative") is not True
        or row["candidate"].get("expected_negative") is not True
    ]
    expected_negative_pairs = sum(
        1
        for row in pairs
        if row["baseline"].get("expected_negative") is True
        and row["candidate"].get("expected_negative") is True
    )
    incomplete_pairs = sum(
        1
        for row in pairs
        if not (
            row["baseline"].get("expected_negative") is True
            and row["candidate"].get("expected_negative") is True
        )
        and not (
            _covered(row["baseline"], "total_tokens")
            and _covered(row["candidate"], "total_tokens")
        )
    )
    incurred_baseline = sum(
        (row["baseline"].get("total_tokens") or 0)
        for row in positive
        if _covered(row["baseline"], "total_tokens")
    )
    incurred_candidate = sum(
        (row["candidate"].get("total_tokens") or 0)
        for row in positive
        if _covered(row["candidate"], "total_tokens")
    )
    incurred_complete = all(
        _covered(row["baseline"], "total_tokens")
        and _covered(row["candidate"], "total_tokens")
        for row in positive
    )
    baseline_correct = sum(1 for row in pairs if row["baseline"].get("correct") is True)
    candidate_correct = sum(1 for row in pairs if row["candidate"].get("correct") is True)
    metrics: dict[str, Any] = {
        "success_rate": candidate_correct / len(pairs) if pairs else None,
        "baseline_success_rate": baseline_correct / len(pairs) if pairs else None,
        "success_rate_delta": (
            (candidate_correct - baseline_correct) / len(pairs) if pairs else None
        ),
        "errors": sum(
            1
            for row in pairs
            if row["baseline"].get("status") == "error"
            or row["candidate"].get("status") == "error"
        ),
        "not_started": sum(
            1
            for row in pairs
            if row["baseline"].get("status") == "not_started"
            or row["candidate"].get("status") == "not_started"
        ),
        "incomplete_pairs": incomplete_pairs,
        "expected_negative_pairs": expected_negative_pairs,
        "incorrect_pairs": sum(
            1
            for row in pairs
            if row["baseline"].get("correct") is False
            or row["candidate"].get("correct") is False
        ),
        "retry_frequency": (
            sum((row["candidate"].get("workflow_retries") or 0) for row in pairs)
            / len(pairs)
            if pairs
            else None
        ),
        "duration_ms": (
            statistics.median(row["candidate"].get("duration_ms") or 0 for row in pairs)
            if pairs
            else None
        ),
        "successful_pairs": covered_pairs,
        "token_delta_median": median(token_deltas),
        "token_delta_evaluable": bool(token_deltas),
        "total_tokens_delta_median": median(token_deltas),
        "input_tokens_delta_median": median(input_deltas),
        "output_tokens_delta_median": median(output_deltas),
        "estimated_input_tokens_delta_median": median(estimated_deltas),
        "logical_calls_delta_median": median(call_deltas),
        "retry_delta_median": median(retry_deltas),
        "duration_delta_median": median(duration_deltas),
        "expansion_delta_median": median(expansion_deltas),
        "baseline_total_tokens": incurred_baseline,
        "candidate_total_tokens": incurred_candidate,
        "incurred_total_tokens_delta": incurred_candidate - incurred_baseline,
        "incurred_tokens_complete": incurred_complete,
        "pairs": len(pairs),
        "cases": _case_deltas(pairs, "total_tokens", "total_tokens"),
        "coverage": {
            "total_tokens_pairs": token_pairs,
            "input_tokens_pairs": len(input_deltas),
            "output_tokens_pairs": len(output_deltas),
            "scheduled_pairs": len(pairs),
        },
    }
    metrics.update(_retrieval_metrics(pairs))
    return metrics


def _declared_schedule(
    run: Mapping[str, Any], *variants: str | None
) -> list[tuple[str, str, int]] | None:
    spec = run.get("spec") or {}
    schedule = spec.get("schedule") if isinstance(spec, Mapping) else None
    if not isinstance(schedule, list) or not schedule:
        return None
    selected: list[tuple[str, str, int]] = []
    for entry in schedule:
        if not isinstance(entry, Mapping):
            return None
        suite = entry.get("suite")
        case_id = entry.get("case_id")
        variant = entry.get("variant")
        repetition = entry.get("repetition")
        if not (
            isinstance(suite, str)
            and isinstance(case_id, str)
            and isinstance(variant, str)
            and isinstance(repetition, int)
        ):
            return None
        if any(item is not None and item != variant for item in variants):
            continue
        selected.append((suite, case_id, repetition))
    return sorted(selected)


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
    suite_filter = baseline.get("suite")
    if suite_filter == "all":
        suite_filter = None
    baseline_rows = [
        row
        for row in baseline.get("rows", [])
        if (suite_filter is None or row.get("suite") == suite_filter)
        and _variant_matches(row, baseline_variant)
    ]
    candidate_rows = [
        row
        for row in candidate.get("rows", [])
        if (suite_filter is None or row.get("suite") == suite_filter)
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
    # Package source identity is a comparability condition except on the
    # explicit implementation-revision axis.
    if axis != "revision":
        left_source = baseline.get("source_revision")
        right_source = candidate.get("source_revision")
        if left_source != right_source:
            result.comparable = False
            result.reasons.append("source_revision_mismatch")
        left_tree = (baseline.get("spec") or {}).get("source_tree_digest")
        right_tree = (candidate.get("spec") or {}).get("source_tree_digest")
        if left_tree and right_tree and left_tree != right_tree:
            result.comparable = False
            result.reasons.append("source_tree_mismatch")
    declared = _declared_schedule(baseline, baseline_variant)
    declared_candidate = _declared_schedule(candidate, candidate_variant)
    if declared is not None or declared_candidate is not None:
        if declared is None or declared_candidate is None:
            result.comparable = False
            result.reasons.append("schedule_declaration_missing")
            declared = declared or declared_candidate or []
        elif declared != declared_candidate:
            result.comparable = False
            result.reasons.append("schedule_mismatch")
            declared = sorted(set(declared) | set(declared_candidate))
    else:
        # Legacy results without a declared schedule remain readable.
        declared = sorted(
            {
                (row.get("suite"), row.get("case_id"), row.get("repetition"))
                for row in baseline_rows
            }
        )
        legacy_candidate = {
            (row.get("suite"), row.get("case_id"), row.get("repetition"))
            for row in candidate_rows
        }
        if set(declared) != legacy_candidate:
            result.comparable = False
            result.reasons.append("schedule_mismatch")
            declared = sorted(set(declared) | legacy_candidate)
    if not declared:
        result.comparable = False
        result.reasons.append("schedule_empty")
    if len({item[0] for item in declared}) > 1:
        result.comparable = False
        result.reasons.append("schedule_mixed_suite")

    for suite, case_id, repetition in declared:
        result.scheduled += 1
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
        result.baseline_correct += int(left.get("correct") is True)
        result.candidate_correct += int(right.get("correct") is True)
        result.pairs += 1
        result.metrics.setdefault("pairs", []).append(
            {
                "case_id": case_id,
                "repetition": repetition,
                "baseline": left,
                "candidate": right,
            }
        )
    pairs = result.metrics.pop("pairs", [])
    result.metrics.update(_metric_values(pairs))
    result.metrics["baseline_correct"] = result.baseline_correct
    result.metrics["candidate_correct"] = result.candidate_correct
    if result.baseline_correct != result.candidate_correct:
        result.reasons.append("correctness_changed")
    if any(
        row["baseline"].get("correct") is not None
        and row["candidate"].get("correct") is not None
        and row["baseline"].get("correct") != row["candidate"].get("correct")
        for row in pairs
    ):
        result.reasons.append("per_case_correctness_changed")
    if thresholds:
        evidence_source = str(
            baseline.get("evidence_source")
            or next(
                (row.get("provenance") for row in baseline_rows if row.get("provenance")),
                "",
            )
        )
        for gate in thresholds:
            outcome = _evaluate_gate(
                gate, result.metrics, result.reasons, evidence_source=evidence_source
            )
            payload = {
                "metric": gate.metric,
                "provenance": gate.provenance,
                "scope": gate.scope,
                "max_regression": gate.max_regression,
                "policy_version": gate.policy_version,
                "observed": outcome.get("observed"),
                "status": outcome["status"],
            }
            if outcome.get("reason"):
                payload["reason"] = outcome["reason"]
            result.gates.append(payload)
            if outcome["status"] == "failed":
                result.failed_gates.append(gate.metric)
            elif outcome["status"] == "unevaluable":
                result.unevaluable_gates.append(gate.metric)
    return result


def _scoped_values(metrics: Mapping[str, Any], scope: str) -> Mapping[str, Any]:
    if scope == "suite":
        return metrics
    case_id = scope.split(":", 1)[1]
    return (metrics.get("cases") or {}).get(case_id) or {}


def _evaluate_gate(
    gate: Gate,
    metrics: Mapping[str, Any],
    reasons: list[str],
    *,
    evidence_source: str,
) -> dict[str, Any]:
    if gate.provenance != evidence_source:
        return {"status": "unevaluable", "observed": None, "reason": "provenance_mismatch"}
    case_values = None
    if gate.scope != "suite":
        case_values = _scoped_values(metrics, gate.scope)
        if not case_values:
            return {"status": "unevaluable", "observed": None, "reason": "scope_missing"}
    if gate.metric in {"correctness", "per_case_correctness"}:
        if "correctness_changed" in reasons:
            return {"status": "failed", "observed": 0}
        if "per_case_correctness_changed" in reasons:
            return {"status": "failed", "observed": 0}
        return {"status": "passed", "observed": 1}
    is_higher_delta = (
        gate.metric.endswith("_delta")
        and gate.metric.startswith(HIGHER_IS_BETTER_DELTA_PREFIXES)
    )
    is_lower_delta = (
        gate.metric.endswith("_delta")
        and gate.metric.startswith(LOWER_IS_BETTER_DELTA_PREFIXES)
    )
    if case_values is not None and (
        gate.metric in LOWER_IS_BETTER or is_lower_delta
    ):
        value = case_values.get("candidate_minus_baseline")
        if value is None:
            return {
                "status": "unevaluable",
                "observed": None,
                "reason": "metric_uncovered",
            }
    else:
        value = metrics.get(gate.metric)
        if value is None:
            return {
                "status": "unevaluable",
                "observed": None,
                "reason": "metric_missing",
            }
    if gate.metric in LOWER_IS_BETTER or is_lower_delta:
        failed = float(value) > gate.max_regression
    elif is_higher_delta:
        # A higher-is-better delta regresses only when it is more negative than
        # the declared tolerance.
        failed = float(value) < -gate.max_regression
    elif gate.metric in HIGHER_IS_BETTER:
        failed = float(value) < gate.max_regression
    else:
        return {"status": "unevaluable", "observed": value, "reason": "metric_unknown"}
    return {"status": "failed" if failed else "passed", "observed": value}
