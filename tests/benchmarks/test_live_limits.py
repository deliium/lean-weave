from __future__ import annotations

import json
from pathlib import Path

import pytest

from lean_weave.benchmark.fixtures import load_manifest
from lean_weave.benchmark.live import validate_live_request
from lean_weave.benchmark.offline import write_operator_config


@pytest.fixture
def live_config(tmp_path):
    manifest = load_manifest("bug_fix")
    config_dir = tmp_path / "configs"
    write_operator_config(config_dir / "bug_fix.yaml", manifest)
    return config_dir


@pytest.fixture
def pricing(tmp_path):
    path = tmp_path / "pricing.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "currency": "USD",
                "as_of": "2026-01-01",
                "source": "fixture",
                "models": {
                    "deepseek-flash": {
                        "uncached_input_per_million": "0.10",
                        "output_per_million": "0.20",
                    }
                },
            }
        )
    )
    return path


def _clean_env(monkeypatch):
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "OPENAI_BASE_URL",
        "DEEPSEEK_BASE_URL",
        "SSL_CERT_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-live-test")


def test_live_requires_explicit_limits(live_config, pricing, monkeypatch):
    _clean_env(monkeypatch)
    with pytest.raises(Exception):
        validate_live_request(
            live=False,
            cases=["bug_fix"],
            config_dir=live_config,
            max_tokens=1000,
            max_cost_usd="1.00",
            pricing_path=pricing,
            deadline_s=10,
            repeats=1,
        )
    with pytest.raises(Exception):
        validate_live_request(
            live=True,
            cases=["bug_fix"],
            config_dir=live_config,
            max_tokens=None,
            max_cost_usd="1.00",
            pricing_path=pricing,
            deadline_s=10,
            repeats=1,
        )


def test_conservative_admission_and_cost_refusal(live_config, pricing, monkeypatch):
    _clean_env(monkeypatch)
    admission = validate_live_request(
        live=True,
        cases=["bug_fix"],
        config_dir=live_config,
        max_tokens=1_000_000,
        max_cost_usd="100.00",
        pricing_path=pricing,
        deadline_s=10,
        repeats=2,
    )
    assert admission.reserved_tokens == 2 * 20_000
    with pytest.raises(Exception):
        validate_live_request(
            live=True,
            cases=["bug_fix"],
            config_dir=live_config,
            max_tokens=100,
            max_cost_usd="100.00",
            pricing_path=pricing,
            deadline_s=10,
            repeats=1,
        )
    with pytest.raises(Exception):
        validate_live_request(
            live=True,
            cases=["bug_fix"],
            config_dir=live_config,
            max_tokens=1_000_000,
            max_cost_usd="0.000001",
            pricing_path=pricing,
            deadline_s=10,
            repeats=1,
        )


def test_unknown_case_and_bad_pricing_are_refused(live_config, pricing, monkeypatch):
    _clean_env(monkeypatch)
    with pytest.raises(Exception):
        validate_live_request(
            live=True,
            cases=["single_file_feature"],
            config_dir=live_config,
            max_tokens=1_000_000,
            max_cost_usd="100.00",
            pricing_path=pricing,
            deadline_s=10,
            repeats=1,
        )
    pricing.write_text('{"version": 1, "currency": "EUR", "models": {}}')
    with pytest.raises(Exception):
        validate_live_request(
            live=True,
            cases=["bug_fix"],
            config_dir=live_config,
            max_tokens=1_000_000,
            max_cost_usd="100.00",
            pricing_path=pricing,
            deadline_s=10,
            repeats=1,
        )


def test_post_settlement_policy_stops_before_next_send(tmp_path):
    import httpx2

    from lean_weave.config import AppConfig
    from lean_weave.metrics.jsonl import JsonlMetricsCollector
    from lean_weave.providers.deepseek import DeepSeekProvider

    calls = []
    settled = []
    policy_called = []

    def handler(request):
        calls.append(request)
        return httpx2.Response(
            429,
            json={
                "error": {"message": "rate"},
                "usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
            },
        )

    def policy(reservation):
        settled.append(reservation.charge_tokens)
        policy_called.append(True)
        raise RuntimeError("stop_live")

    config = AppConfig.model_validate({"llm": {"max_retries": 2}})
    journal = JsonlMetricsCollector(tmp_path / "state")
    provider = DeepSeekProvider(
        config.llm,
        journal,
        feature_token_budget=10_000,
        transport=httpx2.MockTransport(handler),
        api_key="sk-test",
        sleeper=lambda _: None,
        post_settlement_policy=policy,
    )
    try:
        provider.complete(
            {
                "feature_id": "f1",
                "stage": "plan",
                "messages": [{"role": "user", "content": "hello"}],
            }
        )
    except RuntimeError as exc:
        assert str(exc) == "stop_live"
    else:  # pragma: no cover
        raise AssertionError("policy stop was ignored")
    assert len(calls) == 1
    assert policy_called == [True]
    assert journal.reporting_snapshot("f1").attempts[0].status == "error"


def test_environment_contamination_is_refused(live_config, pricing, monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid")
    with pytest.raises(Exception):
        validate_live_request(
            live=True,
            cases=["bug_fix"],
            config_dir=live_config,
            max_tokens=1_000_000,
            max_cost_usd="100.00",
            pricing_path=pricing,
            deadline_s=10,
            repeats=1,
        )
