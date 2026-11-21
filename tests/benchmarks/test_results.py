"""Tests for the versioned benchmark metric projection."""

from __future__ import annotations

import uuid

from lean_weave.metrics.jsonl import JsonlMetricsCollector
from lean_weave.metrics.models import normalize_usage
from lean_weave.schemas import FeatureState, Stage, TokenCounters
from lean_weave.workflow.results import WorkflowStatus
from lean_weave.workflow.state import ControlRecord, JsonCheckpointStore

from benchmarks.results import project_metrics, render_human_summary, write_report


def _collector(tmp_path):
    return JsonlMetricsCollector(tmp_path / "state")


def _reserve(collector, *, feature="f1", stage="plan", ordinal=0, estimated=10, cap=20, budget=1000):
    request_id = uuid.uuid4().hex
    return collector.reserve_attempt(
        feature_id=feature,
        stage=stage,
        model_requested="deepseek-flash",
        request_id=request_id,
        attempt_id=f"{request_id}-a{ordinal}",
        attempt_ordinal=ordinal,
        estimated_input_tokens=estimated,
        output_cap=cap,
        budget_tokens=budget,
    )


def _usage(**kwargs):
    base = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    base.update(kwargs)
    return normalize_usage(base)


def test_consistent_usage_projection_and_no_subset_double_count(tmp_path):
    collector = _collector(tmp_path)
    reservation = _reserve(collector)
    collector.finalize_attempt(
        reservation,
        status="success",
        usage=_usage(
            prompt_cache_hit_tokens=2,
            completion_tokens_details={"reasoning_tokens": 3},
        ),
        model_returned="deepseek-flash",
    )
    report = project_metrics(
        feature_id="f1",
        journal=collector,
        provenance="mock_functional",
    )
    assert report.logical_workflow_calls == 1
    assert report.journal_reservations == 1
    assert report.journal_confirmed_outcomes == 1
    assert report.journal_uncertain_attempts == 0
    assert report.verification_llm_sends == 0
    assert report.tokens.prompt_tokens == 10
    assert report.tokens.completion_tokens == 5
    assert report.tokens.total_processed_tokens == 15
    assert report.tokens.cache_hit_tokens == 2
    assert report.tokens.reasoning_tokens == 3
    # Subsets are not added back into total.
    assert report.tokens.total_processed_tokens == 15
    assert report.attempts[0].tokens.estimated_input_tokens == 10
    assert report.attempts[0].tokens.coverage.estimated_input_tokens is True


def test_incomplete_and_absent_usage(tmp_path):
    collector = _collector(tmp_path)
    reservation = _reserve(collector, estimated=8, cap=12)
    incomplete = normalize_usage({"prompt_tokens": 4, "completion_tokens": 1})
    collector.finalize_attempt(reservation, status="error", usage=incomplete)
    report = project_metrics(feature_id="f1", journal=collector)
    assert report.tokens.total_processed_tokens is None
    assert report.tokens.coverage.total_tokens is False
    assert report.tokens.budget_charge_tokens == 20
    assert report.attempts[0].measured is False

    empty = project_metrics(feature_id="missing", journal=collector)
    assert empty.journal_reservations == 0
    assert empty.tokens.prompt_tokens is None
    assert empty.context_size.available is False


def test_unresolved_reservation_is_not_a_confirmed_send(tmp_path):
    collector = _collector(tmp_path)
    _reserve(collector)
    report = project_metrics(feature_id="f1", journal=collector)
    assert report.journal_uncertain_attempts == 1
    assert report.logical_workflow_calls == 0
    assert report.attempts[0].logical_call is False
    assert report.attempts[0].status == "unresolved"


def test_gateway_retry_vs_workflow_repair_stage(tmp_path):
    collector = _collector(tmp_path)
    first = _reserve(collector, stage="implement", ordinal=0)
    collector.finalize_attempt(
        first,
        status="error",
        usage=_usage(prompt_tokens=3, completion_tokens=0, total_tokens=3),
    )
    second = _reserve(collector, stage="implement", ordinal=1)
    collector.finalize_attempt(second, status="success", usage=_usage())
    repair = _reserve(collector, stage="retry", ordinal=0)
    collector.finalize_attempt(repair, status="success", usage=_usage())
    report = project_metrics(feature_id="f1", journal=collector)
    assert report.gateway_retry_attempts == 1
    retry_stages = [stage for stage in report.stages if stage.workflow_repair_stage]
    assert len(retry_stages) == 1
    assert retry_stages[0].stage == "retry"


def test_checkpoint_retry_count_and_human_summary(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    store = JsonCheckpointStore(root)
    state = FeatureState(
        feature_id="f1",
        stage=Stage.DONE,
        request="x",
        retry_count=2,
        metrics=TokenCounters(prompt_tokens=10, completion_tokens=5, total_tokens=15, llm_calls=1),
    )
    control = ControlRecord(
        feature_id="f1",
        workspace=store.identity,
        terminal_status=WorkflowStatus.SUCCESS,
        terminal_reason="verification_passed",
    )
    store.create_checkpoint(state, control)
    collector = _collector(tmp_path)
    reservation = _reserve(collector)
    collector.finalize_attempt(reservation, status="success", usage=_usage())
    report = project_metrics(
        feature_id="f1",
        journal=collector,
        store=store,
        context_estimated_tokens=42,
        check_executions=2,
        check_passes=1,
        check_failures=1,
    )
    assert report.workflow_retry_count == 2
    assert report.workflow_repair_count == 2
    assert report.context_size.available is True
    assert report.checks.available is True
    text = render_human_summary(report)
    assert "logical_calls=1" in text
    assert "context_size=42" in text
    path = tmp_path / "out.json"
    write_report(path, report)
    assert path.is_file()
