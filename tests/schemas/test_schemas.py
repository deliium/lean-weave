import json

import pytest
from pydantic import ValidationError

from lean_weave.schemas import (
    SCHEMA_VERSION,
    FeaturePlan,
    FeatureState,
    ImplementationTask,
    Stage,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
)
from lean_weave.schemas.models import TRANSITIONS


def test_feature_state_roundtrip_without_chat_history():
    state = FeatureState(
        feature_id="f1",
        request="build X",
        stage=Stage.IMPLEMENT,
        plan=FeaturePlan(
            goal="build X",
            tasks=[
                ImplementationTask(id="1", title="scaffold", files=["src/a.py"]),
            ],
        ),
        active_task_id="1",
    )
    payload = state.model_dump()
    assert "messages" not in payload
    assert "chat" not in payload
    restored = FeatureState.model_validate(payload)
    assert restored.version == SCHEMA_VERSION
    assert restored.plan.tasks[0].status is TaskStatus.PENDING
    # JSON serializable
    json.loads(state.model_dump_json())


def test_verification_result_statuses():
    vr = VerificationResult(status=VerificationStatus.FAIL, blocking=True, blockers=[{"id": "t1"}])
    assert vr.status.value == "fail"


def test_invalid_stage_rejected():
    with pytest.raises(ValidationError):
        FeatureState(feature_id="x", stage="not-a-stage")


def test_transition_table_covers_happy_and_retry():
    assert TRANSITIONS[(Stage.PLAN, "planned")] is Stage.IMPLEMENT
    assert TRANSITIONS[(Stage.VERIFY, "fail")] is Stage.RETRY
    assert TRANSITIONS[(Stage.RETRY, "exhausted")] is Stage.FAILED
