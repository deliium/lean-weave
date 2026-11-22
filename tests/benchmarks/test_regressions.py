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
