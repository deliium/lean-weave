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


def test_paired_admission_snapshots_differ_only_by_context_mode(
    live_config, pricing, monkeypatch
):
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
        variants=["v1", "v2"],
    )
    import yaml

    assert admission.variants == ("v1", "v2")
    assert admission.reserved_tokens == 2 * 2 * 20_000
    assert len(admission.arms) == 2
    payloads = []
    for arm in admission.arms:
        payload = yaml.safe_load(Path(arm["config_path"]).read_text())
        context = payload.pop("context")
        payloads.append((payload, context["workflow_mode"], arm["variant"]))
    assert payloads[0][0] == payloads[1][0]
    assert payloads[0][2:] != payloads[1][2:]
    assert {payload[1] for payload in payloads} == {"v1", "v2-with-v1-fallback"}
    assert all("sk-" not in str(arm) for arm in admission.arms)


def test_paired_admission_rejects_other_variant_selection(
    live_config, pricing, monkeypatch
):
    _clean_env(monkeypatch)
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
            variants=["v2"],
        )


def test_paired_admission_reserves_both_arms_before_launch(
    live_config, pricing, monkeypatch
):
    _clean_env(monkeypatch)
    with pytest.raises(Exception):
        validate_live_request(
            live=True,
            cases=["bug_fix"],
            config_dir=live_config,
            max_tokens=39_999,
            max_cost_usd="100.00",
            pricing_path=pricing,
            deadline_s=10,
            repeats=1,
            variants=["v1", "v2"],
        )
    # A single-arm reservation at the same limit is still permitted.
    validate_live_request(
        live=True,
        cases=["bug_fix"],
        config_dir=live_config,
        max_tokens=40_000,
        max_cost_usd="100.00",
        pricing_path=pricing,
        deadline_s=10,
        repeats=1,
    )


def test_run_live_matrix_alternates_and_marks_not_started(tmp_path, monkeypatch):
    from lean_weave.benchmark import live as live_mod
    from lean_weave.benchmark.runner import run_live_matrix

    calls: list[tuple[str, str, int]] = []

    def fake_case(case_id, scratch, *, variant, context_mode, deadline_at, **kwargs):
        calls.append((case_id, variant, int(deadline_at)))
        if len(calls) > 1:
            return live_mod.CaseOutcome(
                case_id=case_id,
                mode="live_deepseek",
                status="timeout",
                reason="live_timeout",
                expected_outcome="success",
                oracle_ok=None,
                expected_negative=False,
                duration_ms=10,
                error="live_timeout",
                variant=variant,
                context_mode=context_mode,
                coverage={},
            )
        return live_mod.CaseOutcome(
            case_id=case_id,
            mode="live_deepseek",
            status="success",
            reason="verification_passed",
            expected_outcome="success",
            oracle_ok=True,
            expected_negative=False,
            duration_ms=10,
            variant=variant,
            context_mode=context_mode,
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            coverage={"input_tokens": True, "output_tokens": True, "total_tokens": True},
            evidence={"dispatch": "observed"},
        )

    monkeypatch.setattr(live_mod, "run_live_case", fake_case)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-live-test")
    run = run_live_matrix(
        {
            "cases": ["bug_fix"],
            "variants": ["v1", "v2"],
            "arms": [
                {"case_id": "bug_fix", "variant": "v1", "context_mode": "v1", "config_path": str(tmp_path / "v1.yaml")},
                {"case_id": "bug_fix", "variant": "v2", "context_mode": "v2-with-v1-fallback", "config_path": str(tmp_path / "v2.yaml")},
            ],
            "repeats": 2,
            "config_paths": [],
            "max_tokens": 1000,
            "max_cost_usd": "1.00",
            "deadline_s": 10,
        }
    )
    assert [entry["variant"] for entry in run.spec["schedule"][:2]] == ["v1", "v2"]
    assert [entry["variant"] for entry in run.spec["schedule"][2:]] == ["v2", "v1"]
    assert run.rows[0].status == "success"
    assert run.rows[0].correct is True
    assert run.rows[0].total_tokens == 15
    assert run.rows[1].status == "timeout"
    assert all(row.status == "not_started" for row in run.rows[2:])
    assert run.incomplete is True


def test_run_live_case_uses_sub_second_remaining_allowance(tmp_path, monkeypatch):
    import subprocess
    import time

    from lean_weave.benchmark import live as live_mod

    manifest = load_manifest("bug_fix")
    config = write_operator_config(tmp_path / "bug_fix.yaml", manifest)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-live-test")
    real_run = subprocess.run
    observed: list[float] = []

    def fake_run(*args, **kwargs):
        command = args[0]
        if command and Path(command[0]).name in {"wl", "lean-weave"}:
            observed.append(float(kwargs["timeout"]))
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return real_run(*args, **kwargs)

    monkeypatch.setattr(live_mod.subprocess, "run", fake_run)
    outcome = live_mod.run_live_case(
        "bug_fix",
        tmp_path / "scratch",
        config_path=config,
        output_dir=tmp_path,
        deadline_s=30,
        deadline_at=time.monotonic() + 0.4,
    )
    assert outcome.status == "timeout"
    assert outcome.error == "live_timeout"
    assert observed and 0 < observed[0] <= 1.0
