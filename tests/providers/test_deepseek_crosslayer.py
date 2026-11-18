import json
import logging

import httpx2
import pytest

from deepseek_probe.fixtures import FIXTURE_THINKING_TOOLS
from lean_weave.config import AppConfig
from lean_weave.errors import BudgetExceededError
from lean_weave.metrics.budget import estimate_request_tokens
from lean_weave.metrics.jsonl import JsonlMetricsCollector
from lean_weave.providers.deepseek import DeepSeekProvider


REQUEST = {
    "feature_id": "f1",
    "stage": "plan",
    "messages": [{"role": "user", "content": "SECRET-PROMPT"}],
}


def build(tmp_path, handler, *, budget):
    config = AppConfig.model_validate(
        {
            "llm": {"max_retries": 0, "max_output_tokens": 64},
            "workflow": {"feature_token_budget": budget},
        }
    )
    journal = JsonlMetricsCollector(tmp_path / "state")
    provider = DeepSeekProvider(
        config.llm,
        journal,
        feature_token_budget=budget,
        transport=httpx2.MockTransport(handler),
        api_key="sk-test",
    )
    return provider, journal


def test_restart_then_different_stage_denied_before_transport(tmp_path):
    budget = estimate_request_tokens(
        messages=REQUEST["messages"],
        tools=None,
        response_format=None,
        extra={"thinking": {"type": "disabled"}},
    ) + 64

    def ok(request):
        return httpx2.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    first, journal = build(tmp_path, ok, budget=budget)
    first.complete(REQUEST)
    assert journal.spent("f1") == 15

    reopened = JsonlMetricsCollector(tmp_path / "state")
    sent = []

    def no_call(request):
        sent.append(request)
        return httpx2.Response(200, json={})

    config = AppConfig.model_validate(
        {
            "llm": {"max_retries": 0, "max_output_tokens": 64},
            "workflow": {"feature_token_budget": budget},
        }
    )
    second = DeepSeekProvider(
        config.llm,
        reopened,
        feature_token_budget=budget,
        transport=httpx2.MockTransport(no_call),
        api_key="sk-test",
    )
    with pytest.raises(BudgetExceededError):
        second.complete({**REQUEST, "stage": "implement"})
    assert sent == []
    assert reopened.totals("f1").total_tokens == 15


def test_reasoning_tool_and_prompt_payloads_do_not_reach_logs_or_journal(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="lean_weave")

    def handler(request):
        data = json.loads(json.dumps(FIXTURE_THINKING_TOOLS))
        data["created"] = 0
        data["choices"][0]["message"]["reasoning_content"] = "SECRET-REASONING"
        data["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = (
            '{"text": "SECRET-TOOL-PAYLOAD"}'
        )
        return httpx2.Response(200, json=data)

    budget = 100_000
    provider, journal = build(tmp_path, handler, budget=budget)
    tools = [{"type": "function", "function": {"name": "echo", "parameters": {"type": "object"}}}]
    provider.complete({**REQUEST, "tools": tools, "thinking": "enabled"})
    persisted = journal.journal_path.read_text()
    captured = caplog.text
    for payload in ("SECRET-PROMPT", "SECRET-REASONING", "SECRET-TOOL-PAYLOAD"):
        assert payload not in persisted
        assert payload not in captured
    # In-flight responses may carry reasoning; persistence never does.
    assert "reasoning_tokens" in persisted
