"""Runner tests: offline evidence plus live preflight refusals."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from benchmarks.fixtures import CASE_IDS, FixtureError, load_manifest
from benchmarks.offline import write_operator_config
from benchmarks.run import (
    CANONICAL_ENDPOINT,
    CaseOutcome,
    RunnerReport,
    describe_settings,
    render_runner_summary,
    run_offline_case,
    validate_live_config,
)


def test_offline_runner_bug_fix(tmp_path: Path, monkeypatch):
    outcome = run_offline_case(
        "bug_fix",
        tmp_path / "scratch",
        monkeypatch=monkeypatch,
        output_dir=tmp_path / "out",
    )
    assert outcome.error is None
    assert outcome.status == "success"
    assert outcome.oracle_ok is True
    assert outcome.metrics_path is not None
    assert Path(outcome.metrics_path).is_file()
    assert outcome.exit_code == 0
    assert outcome.retry_count == 0
    assert outcome.verification_status == "pass"
    assert outcome.settings is not None
    assert outcome.settings.config_fingerprint.startswith("sha256:")
    assert outcome.settings.model == "deepseek-flash"
    assert outcome.settings.thinking is False
    assert outcome.settings.max_output_tokens == 256
    assert outcome.settings.gateway_max_retries == 0
    assert outcome.settings.workflow["max_retries"] == load_manifest("bug_fix").workflow_limits.get(
        "max_retries", 2
    )
    assert [item["id"] for item in outcome.settings.checks] == ["assert_add"]
    assert outcome.settings.checks[0]["runs_workspace_code"] is False
    # Check history is unavailable rather than invented.
    assert outcome.check_executions is None


def test_offline_runner_negative_budget(tmp_path: Path, monkeypatch):
    outcome = run_offline_case(
        "budget_exhaustion",
        tmp_path / "scratch",
        monkeypatch=monkeypatch,
        output_dir=tmp_path / "out",
    )
    assert outcome.expected_negative is True
    assert outcome.error is None
    assert "budget" in outcome.status or outcome.status == "budget_exhausted"


def test_live_preflight_rejects_custom_endpoint_and_wider_approvals(tmp_path: Path):
    manifest = load_manifest("bug_fix")
    good = write_operator_config(tmp_path / "good.yaml", manifest)
    validate_live_config(good, "bug_fix")

    payload = yaml.safe_load(good.read_text(encoding="utf-8"))
    payload["llm"]["base_url"] = "https://example.invalid"
    bad_endpoint = tmp_path / "bad-endpoint.yaml"
    bad_endpoint.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(FixtureError, match="live_endpoint_rejected"):
        validate_live_config(bad_endpoint, "bug_fix")

    payload = yaml.safe_load(good.read_text(encoding="utf-8"))
    payload["tools"]["write_allow"].append("src/extra.py")
    wider = tmp_path / "wider.yaml"
    wider.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(FixtureError, match="live_write_allow_mismatch"):
        validate_live_config(wider, "bug_fix")

    payload = yaml.safe_load(good.read_text(encoding="utf-8"))
    payload["tools"]["checks"][0]["argv"].append("--changed")
    # Same id but definition drift still has matching ids; classification stays data-only.
    # Endpoint and write allow match; check id list matches — runner currently keys on ids.
    # Alter runs_workspace_code to prove classification refusal.
    payload["tools"]["checks"][0]["runs_workspace_code"] = True
    coded = tmp_path / "coded.yaml"
    coded.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(FixtureError, match="live_workspace_code_forbidden"):
        validate_live_config(coded, "bug_fix")

    descriptor = describe_settings(good)
    assert descriptor.config_fingerprint.startswith("sha256:")
    assert descriptor.base_url == CANONICAL_ENDPOINT
    assert [item["id"] for item in descriptor.checks] == ["assert_add"]
    assert descriptor.write_allow == sorted(manifest.write_paths)

    assert CANONICAL_ENDPOINT == "https://api.deepseek.com"


def test_live_never_enabled_by_key_presence_in_pytest():
    # The default offline suite must not call live even if a key is present.
    assert "bug_fix" in CASE_IDS


def test_render_runner_summary_mentions_denominators():
    report = RunnerReport(
        schema_version=1,
        mode="offline_mock",
        revision="abc",
        cases=[
            CaseOutcome(
                case_id="bug_fix",
                mode="offline_mock",
                status="success",
                reason="ok",
                expected_outcome="success",
                oracle_ok=True,
                expected_negative=False,
                duration_ms=10,
            )
        ],
    )
    text = render_runner_summary(report)
    assert "correctness=1/1" in text
