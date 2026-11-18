import pytest
from pydantic import ValidationError

from lean_weave.config import AppConfig


@pytest.mark.parametrize("payload", [
    {"version": 2}, {"version": True}, {"version": 1.0},
    {"unknown": 1}, {"workflow": {"state_dir": "/tmp"}},
    {"workflow": {"state_dir": "../escape"}}, {"workflow": {"state_dir": ""}},
    {"workflow": {"max_retries": -1}}, {"workflow": {"max_retries": "2"}},
    {"workflow": {"feature_token_budget": 0}},
    {"workflow": {"feature_token_budget": -1}},
    {"workflow": {"feature_token_budget": "20000"}},
    {"llm": {"api_key_env": "bad-name"}}, {"llm": {"timeout_s": 0}},
    {"llm": {"timeout_s": float("inf")}}, {"llm": {"api_key": "secret"}},
    {"llm": {"thinking": "yes"}},
    {"llm": {"max_retries": -1}}, {"llm": {"max_retries": 6}},
    {"llm": {"max_retries": "2"}},
    {"llm": {"max_output_tokens": 0}}, {"llm": {"max_output_tokens": -1}},
    {"llm": {"max_output_tokens": "1024"}},
    {"llm": {"base_url": "http://api.deepseek.com"}},
    {"llm": {"base_url": "api.deepseek.com"}},
    {"llm": {"base_url": "https://user:pass@api.deepseek.com"}},
    {"llm": {"base_url": "https://api.deepseek.com?x=1"}},
    {"llm": {"base_url": "https://api.deepseek.com#frag"}},
    {"llm": {"base_url": "https://api.deepseek.com:bad"}},
    {"llm": {"base_url": " https://api.deepseek.com"}},
])
def test_invalid_config(payload):
    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_default_and_partial_config():
    config = AppConfig()
    assert config.workflow.state_dir == ".lean-weave"
    assert config.llm.max_retries == 2
    assert config.llm.max_output_tokens == 1024
    assert config.workflow.feature_token_budget == 20000
    assert AppConfig.model_validate({"project": {"name": "demo"}}).project.name == "demo"
    assert AppConfig.model_validate(
        {"llm": {"base_url": "https://gateway.example.com/v1"}}
    ).llm.base_url == "https://gateway.example.com/v1"
