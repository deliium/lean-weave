from __future__ import annotations

import json

import pytest

from lean_weave.benchmark.compare import (
    ComparisonError,
    Gate,
    compare_runs,
    load_thresholds,
)


def _row(
    *,
    case: str = "bug_fix",
    variant: str = "v1",
    correct: bool = True,
    tokens: int = 40,
    fingerprint: str = "sha256:abc",
    head: str = "a" * 40,
    conditions: dict | None = None,
    retries: int = 0,
    coverage: dict | None = None,
) -> dict:
    return {
        "arm_id": f"{case}:{variant}",
        "suite": "workflow",
        "case_id": case,
        "variant": variant,
        "repetition": 1,
        "correct": correct,
        "total_tokens": tokens,
        "coverage": coverage
        if coverage is not None
        else {"input_tokens": True, "output_tokens": True, "total_tokens": True},
        "fixture_fingerprint": fingerprint,
        "origin_head": head,
        "provenance": "offline_scripted",
        "conditions": conditions or {"context_mode": variant},
        "workflow_retries": retries,
        "duration_ms": 100,
        "status": "success" if correct else "failed",
    }


def _run(rows: list[dict], revision: str = "rev") -> dict:
    return {"schema_version": 1, "revision": revision, "suite": "workflow", "rows": rows}


def test_compare_pairs_schedules_and_reports_token_delta():
    baseline = _run([_row(variant="v1", tokens=50)])
    candidate = _run([_row(variant="v2", tokens=40)])
    result = compare_runs(baseline, candidate, axis="strategy")
    assert result.comparable is True
    assert result.pairs == 1
    assert result.metrics["token_delta_median"] == -10
    assert result.metrics["successful_pairs"] == 1


def test_schedule_mismatch_is_incompatible():
    baseline = _run([_row(variant="v1")])
    candidate = _run([_row(variant="v2", case="other")])
    result = compare_runs(baseline, candidate, axis="strategy")
    assert result.comparable is False
    assert "schedule_mismatch" in result.reasons


def test_fixture_or_revision_drift_is_incompatible():
    baseline = _run([_row(variant="v1")])
    candidate = _run([_row(variant="v2", fingerprint="sha256:other")])
    assert compare_runs(baseline, candidate, axis="strategy").comparable is False
    candidate = _run([_row(variant="v2", head="b" * 40)])
    assert compare_runs(baseline, candidate, axis="strategy").comparable is False


def test_failed_arm_only_removes_affected_token_delta():
    baseline = _run([_row(variant="v1")])
    candidate = _run([_row(variant="v2", correct=False)])
    result = compare_runs(baseline, candidate, axis="strategy")
    assert result.comparable is True
    assert result.metrics["token_delta_evaluable"] is False
    assert "correctness_changed" in result.reasons


def test_missing_coverage_is_unevaluable_gate():
    baseline = _run([_row(variant="v1")])
    candidate = _run([_row(variant="v2", coverage={"total_tokens": False})])
    gate = Gate(metric="token_delta_median", provenance="offline_scripted", scope="suite", max_regression=0)
    result = compare_runs(baseline, candidate, axis="strategy", thresholds=[gate])
    assert result.gates[0]["status"] == "unevaluable"
    assert result.unevaluable_gates


def test_threshold_policy_round_trip_and_duplicate_rejection(tmp_path):
    path = tmp_path / "thresholds.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "gates": [
                    {
                        "metric": "token_delta_median",
                        "provenance": "offline_scripted",
                        "scope": "suite",
                        "max_regression": 0,
                    }
                ],
            }
        )
    )
    gates = load_thresholds(path)
    assert gates[0].metric == "token_delta_median"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "gates": [
                    {
                        "metric": "token_delta_median",
                        "provenance": "offline_scripted",
                        "scope": "suite",
                        "max_regression": 0,
                    },
                    {
                        "metric": "token_delta_median",
                        "provenance": "offline_scripted",
                        "scope": "suite",
                        "max_regression": 1,
                    },
                ],
            }
        )
    )
    with pytest.raises(ComparisonError):
        load_thresholds(path)


def _scheduled_run(
    rows: list[dict],
    *,
    schedule: list[tuple[str, str, int]],
    revision: str = "rev",
    source_revision: str = "src1",
    evidence_source: str = "offline_scripted",
    suite: str = "workflow",
) -> dict:
    return {
        "schema_version": 1,
        "revision": revision,
        "source_revision": source_revision,
        "evidence_source": evidence_source,
        "suite": suite,
        "spec": {
            "schedule": [
                {
                    "suite": suite,
                    "case_id": case,
                    "variant": variant,
                    "repetition": repetition,
                }
                for case, variant, repetition in schedule
            ]
        },
        "rows": rows,
    }


def test_covered_total_without_optional_subsets_is_eligible():
    baseline = _run([_row(variant="v1", tokens=50)])
    candidate = _run([_row(variant="v2", tokens=40)])
    for row in baseline["rows"] + candidate["rows"]:
        row["coverage"] = {"total_tokens": True, "input_tokens": False}
    result = compare_runs(baseline, candidate, axis="strategy")
    assert result.comparable is True
    assert result.metrics["token_delta_median"] == -10
    assert result.metrics["successful_pairs"] == 1


def test_both_sides_missing_same_scheduled_row_is_incompatible():
    baseline = _scheduled_run(
        [_row(variant="v1")],
        schedule=[("bug_fix", "v1", 1), ("single_file_feature", "v1", 1)],
    )
    candidate = _scheduled_run(
        [_row(variant="v2")],
        schedule=[("bug_fix", "v2", 1), ("single_file_feature", "v2", 1)],
    )
    result = compare_runs(baseline, candidate, axis="strategy")
    assert result.comparable is False
    assert "arm_missing" in result.reasons
    assert result.scheduled == 2


def test_not_started_row_is_incomplete_but_still_scheduled():
    baseline = _run([_row(variant="v1")])
    candidate = _run([_row(variant="v2")])
    baseline["rows"][0]["status"] = "not_started"
    candidate["rows"][0]["status"] = "not_started"
    result = compare_runs(baseline, candidate, axis="strategy")
    assert result.comparable is True
    assert result.scheduled == 1
    assert result.metrics["not_started"] == 1


def test_provenance_and_source_revision_drift_is_incompatible():
    baseline = _run([_row(variant="v1")], revision="rev")
    candidate = _run([_row(variant="v2")], revision="rev")
    candidate["source_revision"] = "different"
    result = compare_runs(baseline, candidate, axis="strategy")
    assert result.comparable is False
    assert "source_revision_mismatch" in result.reasons


def test_axis_rejects_unrelated_condition_drift():
    baseline = _run([_row(variant="v2_default", conditions={"strategy": "v2", "map_tokens": 100, "source_tokens": 800})])
    candidate = _run([_row(variant="v2_tight", conditions={"strategy": "v2", "map_tokens": 100, "source_tokens": 512})])
    result = compare_runs(baseline, candidate, axis="budget")
    assert result.comparable is True
    candidate["rows"][0]["conditions"]["map_tokens"] = 50
    result = compare_runs(baseline, candidate, axis="budget")
    assert result.comparable is False
    assert "condition_drift" in result.reasons


def test_correctness_swap_fails_required_gate():
    baseline = _run(
        [
            _row(case="a", variant="v1", correct=True),
            _row(case="b", variant="v1", correct=False),
        ]
    )
    candidate = _run(
        [
            _row(case="a", variant="v2", correct=False),
            _row(case="b", variant="v2", correct=True),
        ]
    )
    gate = Gate(
        metric="correctness",
        provenance="offline_scripted",
        scope="suite",
        max_regression=0,
        policy_version=2,
    )
    result = compare_runs(baseline, candidate, axis="strategy", thresholds=[gate])
    assert result.failed_gates == ["correctness"]


def test_v2_policy_evaluates_case_scope_delta():
    baseline = _run([_row(case="a", variant="v1", tokens=50)])
    candidate = _run([_row(case="a", variant="v2", tokens=60)])
    gate = Gate(
        metric="total_tokens_delta_median",
        provenance="offline_scripted",
        scope="case:a",
        max_regression=0,
        policy_version=2,
    )
    result = compare_runs(baseline, candidate, axis="strategy", thresholds=[gate])
    assert result.gates[0]["status"] == "failed"
    assert result.gates[0]["observed"] == 10


def test_retrieval_only_comparison_pairs_rows():
    def retrieval_row(variant: str, precision: float) -> dict:
        return {
            "arm_id": f"dev_auth:{variant}",
            "suite": "retrieval",
            "case_id": "dev_auth",
            "variant": variant,
            "repetition": 1,
            "correct": None,
            "provenance": "local_estimate",
            "fixture_fingerprint": "sha256:tree",
            "conditions": {"strategy": "v1" if variant == "v1_default" else "v2"},
            "retrieval": {
                "split": "development",
                "ranking": {
                    "files": {"P@1": precision, "R@1": precision},
                    "symbols": {"P@1": precision, "R@1": precision},
                },
                "pack": {"selected_source_tokens": 100, "missing_dependency_count": 0},
            },
            "status": "complete",
        }

    baseline = _scheduled_run([retrieval_row("v1_default", 0.5)], schedule=[("dev_auth", "v1_default", 1)], suite="retrieval")
    candidate = _scheduled_run([retrieval_row("v2_default", 0.75)], schedule=[("dev_auth", "v2_default", 1)], suite="retrieval")
    baseline["evidence_source"] = "local_estimate"
    candidate["evidence_source"] = "local_estimate"
    result = compare_runs(baseline, candidate, axis="strategy")
    assert result.comparable is True
    assert result.metrics["file_precision_at_1_development"] == 0.75
    assert result.metrics["file_precision_at_1_development_delta"] == 0.25


def test_retrieval_worst_case_gate_catches_hidden_swap():
    def retrieval_row(case: str, variant: str, missing: int) -> dict:
        return {
            "arm_id": f"{case}:{variant}",
            "suite": "retrieval",
            "case_id": case,
            "variant": variant,
            "repetition": 1,
            "correct": None,
            "provenance": "local_estimate",
            "fixture_fingerprint": "sha256:tree",
            "conditions": {"strategy": "v1" if variant == "v1_default" else "v2"},
            "retrieval": {
                "split": "development",
                "ranking": {
                    "files": {"P@1": 1.0, "R@1": 1.0},
                    "symbols": {"P@1": 1.0, "R@1": 1.0},
                },
                "pack": {
                    "selected_source_tokens": 100,
                    "missing_dependency_count": missing,
                    "relevant_token_ratio": 1.0,
                },
            },
            "status": "complete",
        }

    baseline = _scheduled_run(
        [
            retrieval_row("a", "v1_default", 1),
            retrieval_row("b", "v1_default", 0),
        ],
        schedule=[("a", "v1_default", 1), ("b", "v1_default", 1)],
        suite="retrieval",
    )
    candidate = _scheduled_run(
        [
            retrieval_row("a", "v2_default", 0),
            retrieval_row("b", "v2_default", 1),
        ],
        schedule=[("a", "v2_default", 1), ("b", "v2_default", 1)],
        suite="retrieval",
    )
    baseline["evidence_source"] = "local_estimate"
    candidate["evidence_source"] = "local_estimate"
    gate = Gate(
        metric="missing_dependency_count_development_worst_delta",
        provenance="local_estimate",
        scope="suite",
        max_regression=0,
        policy_version=2,
    )
    result = compare_runs(baseline, candidate, axis="strategy", thresholds=[gate])
    assert result.metrics["missing_dependency_count_development_delta"] == 0
    assert result.metrics["missing_dependency_count_development_worst_delta"] == 1
    assert result.metrics["missing_dependency_count_development_total_delta"] == 0
    assert result.failed_gates == ["missing_dependency_count_development_worst_delta"]
