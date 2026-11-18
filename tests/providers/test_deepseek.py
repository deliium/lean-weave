import json
import logging
from copy import deepcopy

import httpx2
import pytest
from pydantic import BaseModel

from deepseek_probe.fixtures import FIXTURE_JSON, FIXTURE_THINKING_TOOLS, FIXTURE_TOOL_CALL
from lean_weave.config import AppConfig
from lean_weave.contracts._types import PROBE_FEATURE_ID
from lean_weave.errors import (
    BudgetExceededError,
    ConfigError,
    InvalidCompletionError,
    ProviderError,
    UnsupportedLLMRequest,
)
from lean_weave.metrics.jsonl import JsonlMetricsCollector
from lean_weave.providers.deepseek import DeepSeekProvider


class Result(BaseModel):
    ok: bool
    stage: str


def payload(fixture, **overrides):
    data = deepcopy(fixture)
    data["created"] = 0
    data.update(overrides)
    return data


def make_provider(tmp_path, handler, *, retries=2, output=1024, budget=100_000, thinking=False, key="sk-test"):
    config = AppConfig.model_validate(
        {
            "llm": {
                "max_retries": retries,
                "max_output_tokens": output,
                "thinking": thinking,
            },
            "workflow": {"feature_token_budget": budget},
        }
    )
    journal = JsonlMetricsCollector(tmp_path / "state")
    sleeps = []
    provider = DeepSeekProvider(
        config.llm,
        journal,
        feature_token_budget=budget,
        transport=httpx2.MockTransport(handler),
        api_key=key,
        sleeper=sleeps.append,
    )
    return provider, journal, sleeps


def plain_request(**overrides):
    request = {
        "feature_id": "f1",
        "stage": "plan",
        "messages": [{"role": "user", "content": "hello"}],
    }
    request.update(overrides)
    return request


def test_request_shape_usage_round_trip_and_no_history(tmp_path):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx2.Response(200, json=payload(FIXTURE_JSON))

    provider, journal, _ = make_provider(tmp_path, handler)
    first = provider.complete(plain_request())
    second = provider.complete(plain_request())
    assert len(sent) == 2
    for request in sent:
        body = json.loads(request.content)
        assert body == {
            "model": "deepseek-flash",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 1024,
            "thinking": {"type": "disabled"},
        }
        assert request.headers["authorization"] == "Bearer sk-test"
    assert first["usage"].total_tokens == 42
    assert first["usage_reported"] is True
    assert first["model"] == "deepseek-flash"
    assert second["message"]["content"] == '{"ok": true, "stage": "plan"}'
    assert journal.totals("f1").total_tokens == 84


def test_json_object_modes_validate_locally(tmp_path):
    def handler(request):
        return httpx2.Response(200, json=payload(FIXTURE_JSON))

    provider, journal, _ = make_provider(tmp_path, handler)
    response = provider.complete(
        plain_request(response_format={"type": "json_object"}, result_model=Result)
    )
    assert response["structured"] == {"ok": True, "stage": "plan"}
    assert journal.totals("f1").total_tokens == 42


@pytest.mark.parametrize("content,code", [
    ("   ", "blank_json"),
    ("not-json", "malformed_json"),
    ("[1, 2]", "non_object"),
    ('{"ok": "not-bool", "stage": 1}', "schema_invalid"),
])
def test_invalid_json_is_charged_before_rejection(tmp_path, content, code):
    def handler(request):
        data = payload(FIXTURE_JSON)
        data["choices"][0]["message"]["content"] = content
        return httpx2.Response(200, json=data)

    provider, journal, sleeps = make_provider(tmp_path, handler, retries=0)
    with pytest.raises(InvalidCompletionError) as error:
        provider.complete(
            plain_request(response_format={"type": "json_object"}, result_model=Result)
        )
    assert getattr(error.value, "code") == code
    summary = journal.summary("f1")
    assert summary.attempts_total == 1
    assert summary.attempts[0].status == "invalid"
    assert summary.budget_charge == 42
    assert sleeps == []


def test_blank_json_retry_is_documented_and_bounded(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        data = payload(FIXTURE_JSON)
        data["choices"][0]["message"]["content"] = ""
        return httpx2.Response(200, json=data)

    provider, journal, sleeps = make_provider(tmp_path, handler, retries=1)
    with pytest.raises(InvalidCompletionError):
        provider.complete(plain_request(response_format={"type": "json_object"}))
    assert len(calls) == 2
    assert sleeps == [0.25]
    summary = journal.summary("f1")
    assert summary.retry_sends == 1
    assert summary.failed_attempts == 2


def test_tool_call_and_thinking_replay(tmp_path):
    sent = []

    def handler(request):
        sent.append(request)
        if len(sent) == 1:
            return httpx2.Response(200, json=payload(FIXTURE_THINKING_TOOLS))
        return httpx2.Response(200, json=payload(FIXTURE_JSON))

    provider, journal, _ = make_provider(tmp_path, handler, thinking=True)
    tools = [{"type": "function", "function": {"name": "echo", "parameters": {"type": "object"}}}]
    first = provider.complete(
        plain_request(
            tools=tools,
            thinking="enabled",
            reasoning_effort="low",
            max_output_tokens=64,
            options={"thinking": "enabled"},
        )
    )
    message = first["message"]
    assert message["content"] in (None, "")
    assert message["tool_calls"][0]["id"] == "call_offline_2"
    assert message["reasoning_content"]
    second = provider.complete(
        plain_request(
            tools=tools,
            thinking="enabled",
            options={"thinking": "enabled"},
            messages=[
                {"role": "user", "content": "call echo"},
                message,
                {
                    "role": "tool",
                    "tool_call_id": "call_offline_2",
                    "content": "hi",
                },
            ],
        )
    )
    assert second["message"]["content"] == '{"ok": true, "stage": "plan"}'
    body = json.loads(sent[1].content)
    assert body["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in body
    assert json.loads(sent[0].content)["reasoning_effort"] == "low"
    assert body["messages"][1]["reasoning_content"] == message["reasoning_content"]
    assert body["messages"][1]["tool_calls"][0]["id"] == "call_offline_2"
    assert body["messages"][2]["tool_call_id"] == "call_offline_2"
    assert journal.summary("f1").attempts_total == 2


def test_malformed_thinking_replay_rejected_without_transport(tmp_path):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx2.Response(200, json=payload(FIXTURE_JSON))

    provider, _, _ = make_provider(tmp_path, handler, thinking=True)
    tools = [{"type": "function", "function": {"name": "echo", "parameters": {"type": "object"}}}]
    with pytest.raises(UnsupportedLLMRequest):
        provider.complete(
            plain_request(
                tools=tools,
                thinking="enabled",
                messages=[
                    {"role": "user", "content": "call echo"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "echo", "arguments": "{}"},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
                ],
            )
        )
    assert sent == []


@pytest.mark.parametrize("overrides", [
    {"tools": [{"type": "function", "function": {"name": "x"}}], "response_format": {"type": "json_object"}},
    {"response_format": {"type": "json_schema"}},
    {"result_model": Result},
    {"model": "unknown-model"},
    {"max_output_tokens": 2048},
    {"stage": "probe"},
    {"feature_id": None},
    {"options": {"retry_limit": 3}},
])
def test_unsupported_requests_make_no_call(tmp_path, overrides):
    sent = []

    def handler(raw):
        sent.append(raw)
        return httpx2.Response(200, json=payload(FIXTURE_JSON))

    provider, _, _ = make_provider(tmp_path, handler)
    with pytest.raises(UnsupportedLLMRequest):
        provider.complete(plain_request(**overrides))
    assert sent == []


def test_probe_requires_reserved_feature(tmp_path):
    provider, _, _ = make_provider(tmp_path, lambda request: httpx2.Response(200, json=payload(FIXTURE_JSON)))
    with pytest.raises(UnsupportedLLMRequest):
        provider.complete(plain_request(feature_id="normal", stage="probe"))
    with pytest.raises(UnsupportedLLMRequest):
        provider.complete(plain_request(feature_id=PROBE_FEATURE_ID, stage="plan"))


def test_missing_key_and_secret_url_fail_safely(tmp_path):
    provider, _, _ = make_provider(
        tmp_path, lambda request: httpx2.Response(200, json=payload(FIXTURE_JSON)), key=""
    )
    with pytest.raises(ProviderError, match="API key"):
        provider.complete(plain_request())

    config = AppConfig.model_validate(
        {"llm": {"base_url": "https://api.deepseek.com/sk-sentinel"}}
    )
    journal = JsonlMetricsCollector(tmp_path / "state2")
    provider = DeepSeekProvider(
        config.llm,
        journal,
        transport=httpx2.MockTransport(lambda request: httpx2.Response(200, json={})),
        api_key="sk-sentinel",
    )
    with pytest.raises(ConfigError) as error:
        provider.complete(plain_request())
    assert "sk-sentinel" not in str(error.value)


def test_redirects_are_not_followed(tmp_path):
    sent = []

    def handler(request):
        sent.append(str(request.url))
        return httpx2.Response(302, headers={"location": "https://evil.example/redirect"})

    provider, journal, _ = make_provider(tmp_path, handler, retries=0)
    with pytest.raises(ProviderError):
        provider.complete(plain_request())
    assert sent == ["https://api.deepseek.com/chat/completions"]
    assert journal.summary("f1").failed_attempts == 1


def test_production_client_disables_sdk_retries_and_redirects(tmp_path):
    config = AppConfig.model_validate({})
    journal = JsonlMetricsCollector(tmp_path / "state")
    provider = DeepSeekProvider(config.llm, journal, api_key="sk-test")
    client = provider._ensure_client()
    try:
        assert client.max_retries == 0
        assert client._client.follow_redirects is False
    finally:
        provider.close()


def test_tool_request_with_thinking_disabled_returns_calls(tmp_path):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx2.Response(200, json=payload(FIXTURE_TOOL_CALL))

    provider, journal, _ = make_provider(tmp_path, handler, thinking=False)
    tools = [{"type": "function", "function": {"name": "echo", "parameters": {"type": "object"}}}]
    response = provider.complete(plain_request(tools=tools, thinking="disabled"))
    assert response["message"]["content"] in (None, "")
    assert response["message"]["tool_calls"][0]["id"] == "call_offline_1"
    assert "reasoning_content" not in response["message"]
    body = json.loads(sent[0].content)
    assert body["thinking"] == {"type": "disabled"}
    assert body["tools"] == tools
    assert journal.summary("f1").attempts_total == 1


def test_incomplete_usage_warns_without_promoting_estimate(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="lean_weave")

    def handler(request):
        data = payload(FIXTURE_JSON)
        data["usage"] = {"prompt_tokens": 10, "completion_tokens": 5}
        return httpx2.Response(200, json=data)

    provider, journal, _ = make_provider(tmp_path, handler, retries=0)
    provider.complete(plain_request())
    assert any(
        record.levelno == logging.WARNING and "incomplete_usage" in record.getMessage()
        for record in caplog.records
    )
    summary = journal.summary("f1")
    assert summary.measured.total_tokens == 15
    assert summary.measured_charge == 0
    assert summary.estimated_charge > 0


def test_raw_usage_is_preserved_without_lossy_normalization(tmp_path):
    def handler(request):
        data = payload(FIXTURE_JSON)
        data["usage"] = {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "unknown_detail": {"value": None, "nested": [1, 0]},
            "prompt_cache_hit_tokens": 0,
            "prompt_tokens_details": {"cached_tokens": 4},
        }
        return httpx2.Response(200, json=data)

    provider, journal, _ = make_provider(tmp_path, handler)
    provider.complete(plain_request())
    line = journal.journal_path.read_text().splitlines()[-1]
    stored = json.loads(line)["usage"]["raw"]
    assert stored["unknown_detail"] == {"value": None, "nested": [1, 0]}
    assert stored["prompt_cache_hit_tokens"] == 0
    assert stored["prompt_tokens_details"]["cached_tokens"] == 4
