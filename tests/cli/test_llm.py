from copy import deepcopy
from pathlib import Path

import httpx2
import pytest
from typer.testing import CliRunner

from deepseek_probe.fixtures import FIXTURE_JSON
from lean_weave.cli import app
from lean_weave.cli.commands import llm as llm_module
from lean_weave.config import init_config, load_config
from lean_weave.metrics.jsonl import JsonlMetricsCollector
from lean_weave.providers.deepseek import DeepSeekProvider
from lean_weave.schemas import TokenUsage

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LEAN_WEAVE_LOG_LEVEL", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


def payload(**overrides):
    data = deepcopy(FIXTURE_JSON)
    data["created"] = 0
    data.update(overrides)
    return data


def install_provider(monkeypatch, handler):
    def factory(context):
        journal = JsonlMetricsCollector.from_config(context.config)
        return DeepSeekProvider(
            context.config.llm,
            journal,
            feature_token_budget=context.config.workflow.feature_token_budget,
            transport=httpx2.MockTransport(handler),
            api_key="sk-injected-test",
            sleeper=lambda delay: None,
        )

    monkeypatch.setattr(llm_module, "create_llm_provider", factory)


def test_check_success_prints_safe_usage_and_records_probe(monkeypatch):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx2.Response(200, json=payload())

    install_provider(monkeypatch, handler)
    source = init_config()
    result = runner.invoke(app, ["llm", "check"])
    assert result.exit_code == 0, result.output
    assert "OK model=deepseek-flash" in result.stdout
    assert "input_tokens=30" in result.stdout
    assert "output_tokens=12" in result.stdout
    assert "total_tokens=42" in result.stdout
    assert '{"ok": true' not in result.output
    assert len(sent) == 1
    journal = JsonlMetricsCollector.from_config(_config(source))
    summary = journal.summary("__llm_probe__")
    assert summary.attempts_total == 1
    assert summary.attempts[0].stage == "probe"
    assert summary.attempts[0].status == "success"
    assert summary.measured.total_tokens == 42


def _config(source):
    return load_config(source).config


def test_check_forces_one_attempt_despite_yaml_retries(monkeypatch):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx2.Response(429, json={"error": {"message": "slow"}})

    install_provider(monkeypatch, handler)
    source = init_config()
    source.write_text(source.read_text().replace("max_retries: 2", "max_retries: 4"))
    result = runner.invoke(app, ["llm", "check"])
    assert result.exit_code == 1
    assert "rate_limited" in result.stderr
    assert "Traceback" not in result.output
    assert len(sent) == 1
    journal = JsonlMetricsCollector.from_config(_config(source))
    assert journal.summary("__llm_probe__").attempts_total == 1


def test_exhausted_probe_sends_nothing_and_isolates_feature_spend(monkeypatch):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx2.Response(200, json=payload())

    install_provider(monkeypatch, handler)
    source = init_config()
    source.write_text(source.read_text().replace("feature_token_budget: 20000", "feature_token_budget: 1"))
    config = _config(source)
    journal = JsonlMetricsCollector.from_config(config)
    journal.record_usage(
        "normal", "plan", TokenUsage(prompt_tokens=3, completion_tokens=2, total_tokens=5)
    )
    result = runner.invoke(app, ["llm", "check"])
    assert result.exit_code == 1
    assert "budget" in result.stderr.lower()
    assert sent == []
    assert journal.totals("normal").total_tokens == 5
    assert journal.spent("__llm_probe__") == 0


def test_check_missing_key_fails_without_network():
    init_config()
    result = runner.invoke(app, ["llm", "check"])
    assert result.exit_code == 1
    assert "API key" in result.stderr
    assert "Traceback" not in result.output


def test_check_config_and_help_paths():
    assert runner.invoke(app, ["--config", "missing.yaml", "llm", "check"]).exit_code == 3
    Path("bad.yaml").write_text("llm: [")
    for args in (["--config", "bad.yaml", "llm", "--help"], ["--config", "bad.yaml", "llm", "check", "--help"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0
        assert "Usage:" in result.stdout
        assert "Config.load" not in result.stderr


def test_model_text_cannot_echo_key_or_control_characters(monkeypatch):
    sentinel = "sk-cli-secret-sentinel"
    monkeypatch.setenv("DEEPSEEK_API_KEY", sentinel)

    def handler(request):
        return httpx2.Response(
            200,
            json=payload(model=f"deepseek-{sentinel}\x1b[31mEVIL"),
        )

    install_provider(monkeypatch, handler)
    init_config()
    result = runner.invoke(app, ["llm", "check"])
    assert result.exit_code == 0, result.output
    assert sentinel not in result.output
    assert "\x1b" not in result.output
    assert "EVIL" in result.stdout


def test_failure_stderr_does_not_expose_provider_body(monkeypatch):
    def handler(request):
        return httpx2.Response(400, json={"error": {"message": "private provider body"}})

    install_provider(monkeypatch, handler)
    init_config()
    result = runner.invoke(app, ["llm", "check"])
    assert result.exit_code == 1
    assert "rejected_request" in result.stderr
    assert "private provider body" not in result.output
    assert "Traceback" not in result.output
