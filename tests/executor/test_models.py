import json

import pytest
from pydantic import ValidationError

from lean_weave.executor.models import (
    ApplyPatchRequest,
    InspectGitDiffRequest,
    ReadSymbolRequest,
    SearchSymbolsRequest,
    file_revision,
    safe_validation_payload,
)
from lean_weave.verify.models import RunChecksRequest

CANARY = "sk-" + ("b" * 24)


@pytest.mark.parametrize(
    "model,payload",
    [
        (ReadSymbolRequest, {"path": "a.py", "name": "Widget"}),
        (SearchSymbolsRequest, {"query": "Widget", "paths": ["pkg/a.py"], "hit_limit": 3}),
        (InspectGitDiffRequest, {"mode": "staged", "paths": ["a.py"]}),
        (RunChecksRequest, {"check_ids": ["parse"]}),
        (RunChecksRequest, {}),
    ],
)
def test_json_round_trip(model, payload):
    value = model.model_validate(payload)
    assert model.model_validate_json(value.model_dump_json()) == value


def test_failure_payload_drops_canary():
    with pytest.raises(ValidationError) as caught:
        ApplyPatchRequest.model_validate(
            {"path": "../" + CANARY, "expected_revision": "nope", "diff": CANARY, "extra": CANARY}
        )
    rendered = json.dumps(safe_validation_payload(caught.value))
    assert CANARY not in rendered
    assert "nope" not in rendered


def test_revision_is_raw_bytes():
    assert file_revision(b"a\r\nb") != file_revision(b"a\nb")
    assert file_revision(b"same") == file_revision(b"same")
