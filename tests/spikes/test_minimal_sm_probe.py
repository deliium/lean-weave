import tempfile
from pathlib import Path

from minimal_sm_probe import FeatureStateLite, MinimalStateMachine, run_happy_path, run_retry_path
from minimal_sm_probe.probe import Stage


def test_happy_path_with_durable_json():
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp) / "feature.json"
        state = run_happy_path(store)
        assert state.stage is Stage.DONE
        assert store.exists()
        reloaded = FeatureStateLite.from_json(store.read_text())
        assert reloaded.stage is Stage.DONE


def test_retry_path():
    state = run_retry_path()
    assert state.stage is Stage.DONE
    assert state.retry_count == 1


def test_invalid_transition_raises():
    sm = MinimalStateMachine(FeatureStateLite(feature_id="x"))
    try:
        sm.verify(True)
        assert False, "expected error"
    except Exception:
        pass
