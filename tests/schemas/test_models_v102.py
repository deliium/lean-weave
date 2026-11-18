import pytest
from pydantic import ValidationError

from lean_weave.schemas import (
    FeaturePlan, FeatureRequest, FeatureState, ImplementationTask,
    TokenUsage, VerificationResult,
)


@pytest.mark.parametrize("model, payload", [
    (FeatureRequest, {"feature_id": "f1", "description": "build"}),
    (TokenUsage, {"prompt_tokens": 3}),
    (FeaturePlan, {"goal": "build"}),
    (FeatureState, {"feature_id": "f1"}),
    (ImplementationTask, {"id": "1", "title": "build"}),
    (VerificationResult, {"status": "pass"}),
])
def test_json_roundtrip(model, payload):
    value = model(**payload)
    assert model.model_validate_json(value.model_dump_json()) == value


@pytest.mark.parametrize("payload", [
    {"feature_id": "", "description": "build"},
    {"feature_id": "f", "description": ""},
    {"feature_id": "f", "description": "build", "unknown": True},
])
def test_invalid_request(payload):
    with pytest.raises(ValidationError):
        FeatureRequest(**payload)


@pytest.mark.parametrize("field", list(TokenUsage.model_fields))
def test_negative_tokens(field):
    with pytest.raises(ValidationError):
        TokenUsage(**{field: -1})


def test_request_utc_and_defaults():
    assert FeatureRequest(feature_id="f", description="d").created_at.utcoffset().total_seconds() == 0
    with pytest.raises(ValidationError):
        TokenUsage(unknown=1)
