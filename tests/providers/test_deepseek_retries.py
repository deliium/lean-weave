from copy import deepcopy

import httpx2
import pytest

from deepseek_probe.fixtures import FIXTURE_JSON
from lean_weave.config import AppConfig
from lean_weave.errors import BudgetExceededError, ProviderError
from lean_weave.metrics.budget import estimate_request_tokens
from lean_weave.metrics.jsonl import JsonlMetricsCollector
from lean_weave.providers.deepseek import DeepSeekProvider


def response(fixture=FIXTURE_JSON, **overrides):
    data = deepcopy(fixture)
    data["created"] = 0
    data.update(overrides)
    return data


def build(tmp_path, handler, *, retries=2, budget=100_000, output=1024):
    config = AppConfig.model_validate(
        {
            "llm": {"max_retries": retries, "max_output_tokens": output},
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
        api_key="sk-test",
        sleeper=sleeps.append,
    )
    return provider, journal, sleeps


REQUEST = {
    "feature_id": "f1",
    "stage": "plan",
    "messages": [{"role": "user", "content": "hello"}],
}


def expected_charge(output=1024):
    estimate = estimate_request_tokens(
        messages=REQUEST["messages"],
        tools=None,
        response_format=None,
        extra={"thinking": {"type": "disabled"}},
    )
    return estimate + output


def test_retryable_usage_then_success(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx2.Response(
                429,
                json={"error": {"message": "slow down"}, "usage": {"prompt_tokens": 5, "completion_tokens": 0, "total_tokens": 5}},
            )
        return httpx2.Response(200, json=response())

    provider, journal, sleeps = build(tmp_path, handler)
    result = provider.complete(REQUEST)
    assert result["usage"].total_tokens == 42
    assert len(calls) == 2
    assert sleeps == [0.25]
    summary = journal.summary("f1")
    assert summary.retry_sends == 1
    assert summary.failed_attempts == 1
    assert summary.measured.prompt_tokens == 35
    assert summary.measured.completion_tokens == 12
    assert summary.budget_charge == 47


def test_retry_exhaustion_counts_each_sent_attempt(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx2.Response(503, json={"error": {"message": "unavailable"}})

    provider, journal, sleeps = build(tmp_path, handler, retries=1)
    with pytest.raises(ProviderError, match="upstream_unavailable"):
        provider.complete(REQUEST)
    assert len(calls) == 2
    assert len(sleeps) == 1
    summary = journal.summary("f1")
    assert summary.attempts_total == 2
    assert summary.retry_sends == 1
    assert summary.failed_attempts == 2


@pytest.mark.parametrize("status,code", [(400, "rejected_request"), (401, "rejected_request")])
def test_permanent_errors_are_not_retried(tmp_path, status, code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx2.Response(status, json={"error": {"message": "no"}})

    provider, journal, sleeps = build(tmp_path, handler, retries=5)
    with pytest.raises(ProviderError, match=code):
        provider.complete(REQUEST)
    assert len(calls) == 1
    assert sleeps == []


def test_retry_after_is_clamped(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx2.Response(429, headers={"retry-after": "99"}, json={})
        return httpx2.Response(200, json=response())

    provider, journal, sleeps = build(tmp_path, handler)
    provider.complete(REQUEST)
    assert sleeps == [30.0]


def test_timeout_is_retried(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx2.ReadTimeout("timed out")
        return httpx2.Response(200, json=response())

    provider, journal, sleeps = build(tmp_path, handler)
    provider.complete(REQUEST)
    assert len(calls) == 2
    assert sleeps
    assert journal.summary("f1").retry_sends == 1


def test_budget_denies_first_send(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx2.Response(200, json=response())

    provider, journal, sleeps = build(tmp_path, handler, budget=10)
    with pytest.raises(BudgetExceededError):
        provider.complete(REQUEST)
    assert calls == []
    assert sleeps == []
    assert journal.spent("f1") == 0


def test_budget_denies_retry_send(tmp_path):
    calls = []
    budget = expected_charge(output=64)

    def handler(request):
        calls.append(request)
        return httpx2.Response(
            429,
            json={"error": {"message": "slow"}, "usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1}},
        )

    provider, journal, sleeps = build(tmp_path, handler, budget=budget, output=64)
    with pytest.raises(BudgetExceededError):
        provider.complete(REQUEST)
    assert len(calls) == 1
    assert journal.spent("f1") == 1
    assert journal.summary("f1").unresolved_attempts == 0


def test_overshoot_persists_outcome_then_raises(tmp_path):
    budget = expected_charge(output=16)

    def handler(request):
        return httpx2.Response(
            200,
            json=response(
                usage={
                    "prompt_tokens": 5000,
                    "completion_tokens": 5000,
                    "total_tokens": 10000,
                }
            ),
        )

    provider, journal, _ = build(tmp_path, handler, budget=budget, output=16)
    with pytest.raises(BudgetExceededError):
        provider.complete(REQUEST)
    summary = journal.summary("f1")
    assert summary.attempts[0].status == "success"
    assert journal.spent("f1") == 10000
    assert summary.measured.total_tokens == 10000


def test_probe_option_forces_single_attempt(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx2.Response(429, json={})

    provider, journal, _ = build(tmp_path, handler, retries=5)
    with pytest.raises(ProviderError):
        provider.complete(
            {
                **REQUEST,
                "feature_id": "__llm_probe__",
                "stage": "probe",
                "options": {"retry_limit": 0, "thinking": "disabled"},
            }
        )
    assert len(calls) == 1
    assert journal.summary("__llm_probe__").attempts_total == 1
