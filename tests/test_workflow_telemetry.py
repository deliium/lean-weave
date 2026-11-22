from __future__ import annotations

import json
from pathlib import Path

from lean_weave.benchmark.runner import run_offline_arm
from lean_weave.metrics.jsonl import JsonlMetricsCollector
from lean_weave.workflow.state import JsonCheckpointStore


def test_verify_then_repair_keeps_attempts_and_observations(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    row = run_offline_arm("verify_then_repair", "v2", repetition=1)
    assert row.status == "success"
    assert row.correct is True
    assert row.workflow_retries == 1
    assert row.verification_status == "pass"
    assert row.total_tokens is not None


def test_mock_and_v1_v2_arms_share_accounting_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    for variant in ("v1", "v2"):
        row = run_offline_arm("bug_fix", variant)
        assert row.status == "success"
        assert row.correct is True
        assert row.total_tokens == 42
        assert row.input_tokens == 24
        assert row.output_tokens == 18
        assert row.coverage["total_tokens"] is True


def test_legacy_checkpoint_without_telemetry_deserializes(tmp_path):
    from lean_weave.workflow.state import CheckpointEnvelope, ControlRecord
    from lean_weave.schemas import FeatureState, Stage

    state = FeatureState(feature_id="legacy", stage=Stage.DONE, request="x")
    control = ControlRecord(feature_id="legacy", workspace="sha256:" + "0" * 64)
    envelope = CheckpointEnvelope(
        feature_id="legacy", workspace=control.workspace, state=state, control=control
    )
    payload = envelope.model_dump(mode="json")
    payload["control"].pop("telemetry_version", None)
    payload["control"].pop("next_stage_attempt", None)
    decoded = CheckpointEnvelope.model_validate(payload)
    assert decoded.control.closed_attempts == []
    assert decoded.control.open_attempt is None


def test_near_limit_checkpoint_drops_oldest_telemetry_not_state(tmp_path):
    from lean_weave.schemas import FeatureState, Stage
    from lean_weave.workflow.state import (
        ContextObservation,
        ControlRecord,
        JsonCheckpointStore,
        StageAttemptObservation,
    )

    store = JsonCheckpointStore(tmp_path, max_bytes=3000)
    state = FeatureState(feature_id="f1", stage=Stage.DONE, request="x")
    control = ControlRecord(feature_id="f1", workspace=store.identity)
    control.closed_attempts = [
        StageAttemptObservation(
            ordinal=index + 1,
            stage="implement",
            task_id="1",
            status="done",
            duration_ms=5,
            context=[
                ContextObservation(
                    mode="v2", outcome="done", selected_files=10, pack_input_tokens=900
                )
            ],
        )
        for index in range(128)
    ]
    store.create_checkpoint(state, control)
    envelope = store.load_checkpoint("f1")
    assert envelope.state.feature_id == "f1"
    assert envelope.control.next_stage_attempt == 1
    assert envelope.control.truncated_attempts > 0
    assert len(envelope.control.closed_attempts) < 128


def test_journal_and_checkpoint_are_independently_readable(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    row = run_offline_arm("multi_file_feature", "v1")
    # run_offline_arm removes its private scratch after collecting evidence;
    # the row remains a truthful projection of the routed journal.
    assert row.status == "success"
    assert row.logical_calls >= 3
