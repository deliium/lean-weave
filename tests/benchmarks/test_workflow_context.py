"""Matched V1/V2 workflow context comparison over the offline fixture driver."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from benchmarks.offline import (
    ScriptedTransport,
    bind_feature_session,
    install_gateway_transport,
    invoke_cli,
    journal_for,
    parse_view,
    prepare_case,
    success_script,
    worktree_for,
)
from lean_weave.workflow.state import JsonCheckpointStore
from benchmarks.results import (
    BenchmarkMetricReport,
    CheckHistoryProjection,
    ContextSizeProjection,
    TokenRollup,
    compare_workflow_context,
    project_metrics,
)


def _run(case, monkeypatch, mode: str):
    transport = ScriptedTransport(success_script(case.manifest, case.shadow))
    install_gateway_transport(monkeypatch, transport)
    bind_feature_session(monkeypatch, case.origin)
    result = invoke_cli(
        "--config",
        str(case.config_path),
        "feature",
        case.manifest.request,
        "--auto",
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    view = parse_view(result.stdout)
    worktree = worktree_for(case.origin, view["feature_id"])
    report = project_metrics(
        feature_id=view["feature_id"],
        journal=journal_for(worktree),
        store=JsonCheckpointStore(worktree),
        context_mode=mode,
    )
    return report, transport


def test_multi_file_fixture_strictly_reduces_estimated_input(tmp_path: Path, monkeypatch):
    v1_case = prepare_case("multi_file_feature", tmp_path / "v1", context_mode="v1")
    v2_case = prepare_case("multi_file_feature", tmp_path / "v2", context_mode="v2")
    v1, v1_transport = _run(v1_case, monkeypatch, "v1")
    v2, v2_transport = _run(v2_case, monkeypatch, "v2")
    comparison = compare_workflow_context(
        case_id="multi_file_feature", v1=v1, v2=v2
    )
    assert comparison.comparable is True
    assert comparison.strict_reduction is True
    assert comparison.v2_estimated_input_tokens < comparison.v1_estimated_input_tokens
    assert v1_transport.send_count == v2_transport.send_count == 5
    assert {item.stage for item in comparison.stages} >= {"plan", "implement"}
    for stage in comparison.stages:
        assert stage.v1_estimated_input_tokens is not None
        assert stage.v2_estimated_input_tokens is not None


def test_comparison_keeps_missing_usage_explicit():
    v1 = BenchmarkMetricReport(
        schema_version=1,
        feature_id="f",
        provenance="local_estimate",
        workflow_status="success",
        workflow_reason="verification_passed",
        workflow_retry_count=0,
        logical_workflow_calls=1,
        journal_reservations=1,
        journal_confirmed_outcomes=1,
        journal_uncertain_attempts=0,
        gateway_retry_attempts=0,
        workflow_repair_count=0,
        verification_llm_sends=0,
        tokens=TokenRollup(),
        stages=[],
        attempts=[],
        context_size=ContextSizeProjection(),
        checks=CheckHistoryProjection(),
        context_mode="v1",
    )
    v2 = dataclasses.replace(v1, context_mode="v2")
    comparison = compare_workflow_context(case_id="missing", v1=v1, v2=v2)
    assert comparison.strict_reduction is False
    assert comparison.v1_estimated_input_tokens is None
    assert "estimated_input_missing" in comparison.notes
