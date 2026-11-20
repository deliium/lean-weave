import fcntl
import json
import os

import pytest

from lean_weave.errors import StateConflict, StateCorrupt
from lean_weave.schemas import FeaturePlan, FeatureState, ImplementationTask, Stage
from lean_weave.workflow.results import WorkflowStatus, project_result
from lean_weave.workflow.state import (
    ControlRecord,
    EffectKind,
    JsonCheckpointStore,
    PendingEffect,
    checkpoint_capability,
)


def _state(feature_id="f", **kwargs):
    return FeatureState(feature_id=feature_id, **kwargs)


def _control(store, feature_id="f"):
    return ControlRecord(feature_id=feature_id, workspace=store.identity)


def _planned_state(feature_id="f", *, stage=Stage.IMPLEMENT):
    return _state(
        feature_id,
        stage=stage,
        request="add one",
        plan=FeaturePlan(
            goal="add one",
            tasks=[ImplementationTask(id="1", title="edit", files=["a.py"])],
        ),
        active_task_id="1",
    )


def test_roundtrip_save_and_legacy_decode(tmp_path):
    store = JsonCheckpointStore(tmp_path)
    assert checkpoint_capability(store) is True
    state = _planned_state()
    store.save(state)
    assert store.load("f") == state
    envelope = store.load_checkpoint("f")
    assert envelope.envelope_version == 1
    assert envelope.state.stage is Stage.IMPLEMENT

    legacy_path = tmp_path / ".lean-weave" / "workflow" / "legacy.json"
    legacy_path.write_text(_state("legacy").model_dump_json(), encoding="utf-8")
    legacy = store.load("legacy")
    assert legacy.feature_id == "legacy"
    assert legacy.stage is Stage.PLAN
    # A legacy payload is upgraded in place on the next write.
    store.save(legacy)
    assert store.load_checkpoint("legacy").control.terminal_status is None


def test_duplicate_start_and_workspace_identity(tmp_path):
    store = JsonCheckpointStore(tmp_path)
    state = _planned_state()
    store.create_checkpoint(state, _control(store))
    with pytest.raises(StateConflict, match="already"):
        store.create_checkpoint(state, _control(store))

    other_root = tmp_path / "other"
    other_root.mkdir()
    other = JsonCheckpointStore(other_root)
    source = tmp_path / ".lean-weave" / "workflow" / "f.json"
    destination = other_root / ".lean-weave" / "workflow" / "f.json"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(source.read_bytes())
    with pytest.raises(StateCorrupt, match="workspace"):
        other.load("f")


def test_corrupt_torn_and_oversized_files_fail_closed(tmp_path):
    store = JsonCheckpointStore(tmp_path)
    workflow = tmp_path / ".lean-weave" / "workflow"
    workflow.mkdir(parents=True)
    (workflow / "torn.json").write_text('{"feature_id": "torn", "stage": ', encoding="utf-8")
    with pytest.raises(StateCorrupt):
        store.load("torn")
    (workflow / "bad.json").write_text(json.dumps({"unexpected": True}), encoding="utf-8")
    with pytest.raises(StateCorrupt):
        store.load("bad")
    (workflow / "huge.json").write_text("{" + "x" * 1_100_000 + "}", encoding="utf-8")
    with pytest.raises(StateCorrupt):
        store.load("huge")


def test_competing_writer_is_rejected(tmp_path):
    store = JsonCheckpointStore(tmp_path)
    state = _planned_state()
    store.save(state)
    lock_path = tmp_path / ".lean-weave" / "workflow" / "f.lock"
    fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(StateConflict, match="lease"):
            store.save(state)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    store.save(state)


def test_effect_marker_roundtrip(tmp_path):
    store = JsonCheckpointStore(tmp_path)
    store.save(_planned_state())
    effect = PendingEffect(
        kind=EffectKind.PATCH,
        operation_id="op-1",
        task_id="1",
        revision="sha256:" + "a" * 64,
        paths=["a.py"],
        calls=1,
        tokens=12,
    )
    store.mark_effect("f", effect)
    loaded = store.load_checkpoint("f")
    assert loaded.control.pending_effect is not None
    assert loaded.control.pending_effect.operation_id == "op-1"
    assert loaded.control.pending_effect.confirmed is False
    with pytest.raises(StateConflict, match="identity"):
        store.clear_effect("f", "op-2")
    store.clear_effect("f", "op-1")
    assert store.load_checkpoint("f").control.pending_effect is None
    with pytest.raises(StateConflict, match="already"):
        store.mark_effect("f", effect)
        store.mark_effect("f", effect)


def test_symlinked_checkpoint_and_lock_are_rejected(tmp_path):
    store = JsonCheckpointStore(tmp_path)
    store.save(_planned_state())
    target = tmp_path / ".lean-weave" / "workflow" / "f.json"
    real = tmp_path / "real-state.json"
    real.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(real)
    with pytest.raises(StateCorrupt, match="symlink"):
        store.load("f")


def test_terminal_result_projection_survives_reload(tmp_path):
    store = JsonCheckpointStore(tmp_path)
    state = _planned_state(stage=Stage.DONE)
    store.create_checkpoint(state, _control(store))
    store.set_terminal("f", WorkflowStatus.SUCCESS, "verification_passed")

    reloaded = JsonCheckpointStore(tmp_path)
    envelope = reloaded.load_checkpoint("f")
    result = project_result(envelope.state, envelope.control)
    assert result.status is WorkflowStatus.SUCCESS
    assert result.reason == "verification_passed"
    assert result.stage is Stage.DONE
    assert result.tasks[0].id == "1"
    assert reloaded.load("f") == state
