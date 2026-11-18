import pytest
from pydantic import ValidationError

from lean_weave.config import AppConfig


@pytest.mark.parametrize("payload", [
    {"version": 2}, {"version": True}, {"version": 1.0},
    {"unknown": 1}, {"workflow": {"state_dir": "/tmp"}},
    {"workflow": {"state_dir": "../escape"}}, {"workflow": {"state_dir": ""}},
    {"workflow": {"max_retries": -1}}, {"workflow": {"max_retries": "2"}},
    {"llm": {"api_key_env": "bad-name"}}, {"llm": {"timeout_s": 0}},
    {"llm": {"timeout_s": float("inf")}}, {"llm": {"api_key": "secret"}},
    {"llm": {"thinking": "yes"}},
])
def test_invalid_config(payload):
    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_default_and_partial_config():
    assert AppConfig().workflow.state_dir == ".lean-weave"
    assert AppConfig.model_validate({"project": {"name": "demo"}}).project.name == "demo"
