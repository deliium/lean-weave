import json
import logging
import uuid

import pytest

from lean_weave.contracts._types import PROBE_FEATURE_ID
from lean_weave.errors import BudgetExceededError, JournalError
from lean_weave.metrics import jsonl as jsonl_module
from lean_weave.metrics.jsonl import JsonlMetricsCollector
from lean_weave.metrics.models import BudgetReservation, JournalEvent, normalize_usage
from lean_weave.schemas import TokenUsage


def make_collector(tmp_path):
    return JsonlMetricsCollector(tmp_path / "state")


def reserve(collector, *, feature="f1", stage="plan", ordinal=0, budget=1000, estimated=10, cap=20):
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


def usage(prompt=10, completion=5, total=15, **extra):
    return normalize_usage(
        {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            **extra,
        }
    )


def test_reservation_outcome_and_restart(tmp_path):
    collector = make_collector(tmp_path)
    reservation = reserve(collector)
    collector.finalize_attempt(
        reservation,
        status="success",
        usage=usage(reasoning_tokens=0),
        model_returned="deepseek-flash",
        duration_ms=12,
        finish_reason="stop",
    )
    assert collector.totals("f1") == TokenUsage(
        prompt_tokens=10, completion_tokens=5, total_tokens=15,
        reasoning_tokens=0, prompt_cache_hit_tokens=0,
    )
    summary = collector.summary("f1")
    assert summary.attempts_total == 1
    assert summary.retry_sends == 0
    assert summary.budget_charge == 15
    assert summary.measured_charge == 15
    assert summary.estimated_charge == 0
    assert summary.unresolved_attempts == 0
    assert summary.attempts[0].duration_ms == 12

    reopened = make_collector(tmp_path)
    assert reopened.spent("f1") == 15
    assert reopened.totals("f1") == collector.totals("f1")


def test_failed_attempt_usage_and_retry_count(tmp_path):
    collector = make_collector(tmp_path)
    first = reserve(collector, ordinal=0)
    collector.finalize_attempt(
        first, status="error", usage=usage(3, 0, 3), error_code="rate_limited"
    )
    second = reserve(collector, ordinal=1)
    collector.finalize_attempt(second, status="success", usage=usage(10, 5, 15))
    summary = collector.summary("f1")
    assert summary.attempts_total == 2
    assert summary.retry_sends == 1
    assert summary.failed_attempts == 1
    assert summary.measured.prompt_tokens == 13
    assert summary.measured.completion_tokens == 5
    assert summary.measured.total_tokens == 18
    assert summary.budget_charge == 18


def test_incomplete_usage_charges_reservation_not_measured(tmp_path):
    collector = make_collector(tmp_path)
    reservation = reserve(collector, estimated=10, cap=20)
    incomplete = normalize_usage({"prompt_tokens": 4, "completion_tokens": 1})
    collector.finalize_attempt(reservation, status="error", usage=incomplete)
    summary = collector.summary("f1")
    assert summary.budget_charge == 30
    assert summary.measured_charge == 0
    assert summary.estimated_charge == 30
    assert summary.measured.total_tokens == 5


def test_unresolved_reservation_is_charged_and_reported(tmp_path):
    collector = make_collector(tmp_path)
    reserve(collector, estimated=10, cap=20)
    summary = collector.summary("f1")
    assert summary.unresolved_attempts == 1
    assert summary.unresolved_charge == 30
    assert summary.budget_charge == 30
    assert summary.measured.total_tokens == 0
    assert make_collector(tmp_path).spent("f1") == 30


def test_exact_budget_boundary_and_denial(tmp_path):
    collector = make_collector(tmp_path)
    reserve(collector, estimated=10, cap=20, budget=30)
    with pytest.raises(BudgetExceededError):
        reserve(collector, ordinal=1, estimated=1, cap=0, budget=30)
    assert collector.spent("f1") == 30


def test_feature_isolation(tmp_path):
    collector = make_collector(tmp_path)
    first = reserve(collector, feature="f1", budget=30, estimated=10, cap=20)
    collector.finalize_attempt(first, status="success", usage=usage())
    assert collector.spent("f2") == 0
    reserve(collector, feature="f2", budget=1, estimated=1, cap=0)
    assert collector.spent("f1") == 15


def test_competing_collectors_share_one_journal_budget(tmp_path):
    first = make_collector(tmp_path)
    second = make_collector(tmp_path)
    reserve(first, budget=30, estimated=10, cap=20)
    with pytest.raises(BudgetExceededError):
        reserve(second, ordinal=1, budget=30, estimated=1, cap=0)


def test_duplicate_and_conflicting_outcomes(tmp_path):
    collector = make_collector(tmp_path)
    reservation = reserve(collector)
    first = collector.finalize_attempt(reservation, status="success", usage=usage())
    duplicate = collector.finalize_attempt(reservation, status="success", usage=usage())
    assert duplicate.event_id == first.event_id
    lines = collector.journal_path.read_text().splitlines()
    assert sum(json.loads(line)["event"] == "outcome" for line in lines) == 1
    with pytest.raises(JournalError):
        collector.finalize_attempt(reservation, status="error", usage=usage())


def test_orphan_outcome_rejected(tmp_path):
    collector = make_collector(tmp_path)
    orphan = BudgetReservation(
        reservation_id="missing",
        request_id="missing",
        attempt_id="missing",
        attempt_ordinal=0,
        feature_id="f1",
        stage="plan",
        model_requested="deepseek-flash",
        created_at="now",
        estimated_input_tokens=1,
        output_cap=1,
        charge_tokens=2,
    )
    with pytest.raises(JournalError, match="missing"):
        collector.finalize_attempt(orphan, status="success")


def test_partial_trailing_line_fails_closed(tmp_path):
    collector = make_collector(tmp_path)
    collector.journal_path.parent.mkdir(parents=True)
    collector.journal_path.write_text('{"event": "reservation"')
    with pytest.raises(JournalError, match="partial"):
        collector.summary("f1")


def test_orphan_outcome_line_fails_closed(tmp_path):
    collector = make_collector(tmp_path)
    collector.journal_path.parent.mkdir(parents=True)
    event = JournalEvent(
        event="outcome",
        event_id="missing:outcome",
        reservation_id="missing",
        request_id="missing",
        attempt_id="missing",
        feature_id="f1",
        stage="plan",
        model_requested="deepseek-flash",
        created_at="now",
        charge_tokens=1,
        status="success",
    )
    line = json.dumps(event.model_dump(mode="json"), sort_keys=True)
    collector.journal_path.write_text(line + "\n")
    with pytest.raises(JournalError, match="no reservation"):
        collector.summary("f1")


def test_exact_duplicate_lines_are_idempotent(tmp_path):
    collector = make_collector(tmp_path)
    reserve(collector, estimated=10, cap=20)
    line = collector.journal_path.read_text()
    collector.journal_path.write_text(line + line)
    summary = collector.summary("f1")
    assert summary.attempts_total == 1
    assert summary.unresolved_charge == 30


def test_symlink_journal_rejected(tmp_path):
    collector = make_collector(tmp_path)
    collector.journal_path.parent.mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("")
    collector.journal_path.symlink_to(outside)
    with pytest.raises(JournalError, match="symlink"):
        collector.summary("f1")


def test_unavailable_locking_fails_closed(tmp_path, monkeypatch):
    collector = make_collector(tmp_path)
    monkeypatch.setattr(jsonl_module, "_LOCK_WAIT_S", 0.0)
    monkeypatch.setattr(
        jsonl_module.fcntl,
        "flock",
        lambda *args, **kwargs: (_ for _ in ()).throw(BlockingIOError()),
    )
    with pytest.raises(JournalError, match="locked"):
        reserve(collector)


def test_legacy_usage_is_booked_and_aggregated(tmp_path):
    collector = make_collector(tmp_path)
    collector.record_usage(
        "f1", "plan", TokenUsage(prompt_tokens=4, completion_tokens=2, total_tokens=6)
    )
    summary = collector.summary("f1")
    assert summary.measured.total_tokens == 6
    assert summary.budget_charge == 6
    assert summary.attempts[0].status == "legacy"


def test_mixed_legacy_and_gateway_usage_rebuilds_totals_and_spend(tmp_path):
    collector = make_collector(tmp_path)
    reservation = reserve(collector, estimated=10, cap=20)
    collector.finalize_attempt(reservation, status="success", usage=usage(10, 5, 15))
    collector.record_usage(
        "f1", "plan", TokenUsage(prompt_tokens=4, completion_tokens=2, total_tokens=6)
    )

    reopened = make_collector(tmp_path)
    summary = reopened.summary("f1")
    assert summary.attempts_total == 2
    assert summary.measured.prompt_tokens == 14
    assert summary.measured.completion_tokens == 7
    assert summary.measured.total_tokens == 21
    assert summary.budget_charge == 21
    assert summary.measured_charge == 21
    assert summary.estimated_charge == 0
    assert reopened.spent("f1") == 21
    assert {attempt.status for attempt in summary.attempts} == {"success", "legacy"}


def test_probe_attribution_is_reserved(tmp_path):
    collector = make_collector(tmp_path)
    with pytest.raises(JournalError, match="probe"):
        reserve(collector, feature="normal", stage="probe")
    with pytest.raises(JournalError, match="probe"):
        reserve(collector, feature=PROBE_FEATURE_ID, stage="plan")
    reserve(collector, feature=PROBE_FEATURE_ID, stage="probe")


def test_dangling_internal_symlink_fails_closed(tmp_path):
    collector = make_collector(tmp_path)
    collector.journal_path.parent.mkdir(parents=True)
    collector.journal_path.symlink_to(collector.journal_path.parent / "missing.jsonl")
    with pytest.raises(JournalError, match="symlink"):
        collector.spent("f1")
    with pytest.raises(JournalError, match="symlink"):
        collector.summary("f1")


def test_replay_failure_logs_safe_error(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="lean_weave")
    collector = make_collector(tmp_path)
    collector.journal_path.parent.mkdir(parents=True)
    collector.journal_path.write_text('{"event": "reservation"')
    with pytest.raises(JournalError):
        collector.summary("f1")
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert any("[Metrics.replay]" in record.getMessage() for record in errors)
    assert '{"event"' not in caplog.text


def test_append_failure_logs_safe_error(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="lean_weave")
    collector = make_collector(tmp_path)
    monkeypatch.setattr(
        jsonl_module.os,
        "fsync",
        lambda fd: (_ for _ in ()).throw(OSError("disk failure")),
    )
    with pytest.raises(JournalError):
        reserve(collector)
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert any("[Metrics.append]" in record.getMessage() for record in errors)
    assert "disk failure" not in caplog.text


def test_payloads_and_secret_bearing_usage_are_not_persisted(tmp_path):
    collector = make_collector(tmp_path)
    reservation = reserve(collector)
    collector.finalize_attempt(
        reservation, status="success", usage=usage(), error_code="safe"
    )
    text = collector.journal_path.read_text()
    assert "private prompt" not in text
    assert "private chain of thought" not in text
    assert "api_key" not in text
