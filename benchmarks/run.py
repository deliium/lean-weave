"""Offline (default) and explicit live benchmark runner.

Never enables live from key presence alone. Live requires ``--live``, env-only
credentials, a trusted external config snapshot, fixture approvals, and the
canonical DeepSeek endpoint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence

import yaml

from benchmarks.fixtures import (
    CASE_IDS,
    FixtureError,
    load_manifest,
    materialize,
    resolve_check_argv,
    run_all_oracles,
)
from benchmarks.offline import (
    ContentShadow,
    ScriptedTransport,
    bind_feature_session,
    install_gateway_transport,
    installed_wl,
    invoke_cli,
    journal_for,
    out_of_policy_plan_script,
    parse_view,
    plan_completion,
    prepare_case,
    success_script,
    worktree_for,
)
from benchmarks.results import (
    BenchmarkMetricReport,
    project_metrics,
    render_human_summary,
    write_report,
)
from lean_weave.config import load_config
from lean_weave.errors import ConfigError
from lean_weave.workflow.state import JsonCheckpointStore

log = logging.getLogger("lean_weave.benchmarks.run")

RUNNER_SCHEMA_VERSION = 1
CANONICAL_ENDPOINT = "https://api.deepseek.com"
Mode = Literal["offline_mock", "live_deepseek"]


@dataclass
class CaseSettings:
    """Bounded, comparable execution settings persisted with each case."""

    config_fingerprint: str
    model: str | None = None
    thinking: bool | None = None
    base_url: str | None = None
    max_output_tokens: int | None = None
    gateway_max_retries: int | None = None
    workflow: dict[str, Any] = field(default_factory=dict)
    write_allow: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CaseOutcome:
    case_id: str
    mode: Mode
    status: str
    reason: str
    expected_outcome: str
    oracle_ok: bool | None
    expected_negative: bool
    duration_ms: int
    feature_id: str | None = None
    metrics_path: str | None = None
    error: str | None = None
    exit_code: int | None = None
    settings: CaseSettings | None = None
    retry_count: int | None = None
    verification_status: str | None = None
    check_executions: int | None = None
    check_passes: int | None = None
    check_failures: int | None = None


@dataclass
class RunnerReport:
    schema_version: int
    mode: Mode
    revision: str
    cases: list[CaseOutcome] = field(default_factory=list)
    incomplete: bool = False
    live_skipped_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _repo_revision() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return completed.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _fingerprint_config(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"sha256:{digest}"


def describe_settings(path: Path) -> CaseSettings:
    """Project bounded comparable settings from an operator config.

    Uses effective (defaults-merged) values and persists fingerprints plus
    declared caps only; never the config body or credentials.
    """

    try:
        config = load_config(path, required=True).config
    except ConfigError as exc:
        raise FixtureError("settings_config_invalid") from exc
    descriptor = CaseSettings(
        config_fingerprint=_fingerprint_config(path),
        model=config.llm.model,
        thinking=config.llm.thinking,
        base_url=config.llm.base_url,
        max_output_tokens=config.llm.max_output_tokens,
        gateway_max_retries=config.llm.max_retries,
        workflow=config.workflow.model_dump(mode="json"),
        write_allow=sorted(config.tools.write_allow),
        checks=[
            {
                "id": check.id,
                "executable": check.executable,
                "argv": list(check.argv),
                "timeout_s": check.timeout_s,
                "output_bytes_max": check.output_bytes_max,
                "runs_workspace_code": check.runs_workspace_code,
            }
            for check in config.tools.checks
        ],
    )
    log.debug(
        "[FIX] runner_settings fingerprint=%s checks=%s model=%s",
        descriptor.config_fingerprint,
        len(descriptor.checks),
        descriptor.model,
    )
    return descriptor


def validate_live_config(path: Path, case_id: str) -> dict[str, Any]:
    """Fail closed before any billable child launch."""

    manifest = load_manifest(case_id)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise FixtureError("live_config_invalid")
    llm = payload.get("llm") or {}
    if llm.get("base_url") != CANONICAL_ENDPOINT:
        raise FixtureError("live_endpoint_rejected")
    tools = payload.get("tools") or {}
    write_allow = list(tools.get("write_allow") or [])
    if sorted(write_allow) != sorted(manifest.write_paths):
        raise FixtureError("live_write_allow_mismatch")
    checks = list(tools.get("checks") or [])
    check_ids = [item.get("id") for item in checks if isinstance(item, dict)]
    if check_ids != list(manifest.check_ids):
        raise FixtureError("live_check_ids_mismatch")
    for check, expected in zip(checks, manifest.checks, strict=True):
        if bool(check.get("runs_workspace_code")):
            raise FixtureError("live_workspace_code_forbidden")
        if check.get("id") != expected.id:
            raise FixtureError("live_check_mismatch")
        if bool(check.get("runs_workspace_code")) != expected.runs_workspace_code:
            raise FixtureError("live_check_classification_mismatch")
        expected_argv = resolve_check_argv(expected)
        if list(check.get("argv") or []) != expected_argv:
            raise FixtureError("live_check_argv_mismatch")
        if check.get("executable") != expected_argv[0]:
            raise FixtureError("live_check_executable_mismatch")
    # Credentials must not appear in the config file.
    text = path.read_text(encoding="utf-8")
    if "sk-" in text or "api_key:" in text.lower():
        raise FixtureError("live_config_contains_secret")
    log.info("[Benchmark.run] live_config=validated case=%s", case_id)
    return payload


def run_offline_case(
    case_id: str,
    scratch: Path,
    *,
    monkeypatch: Any,
    output_dir: Path,
) -> CaseOutcome:
    started = time.monotonic()
    case = prepare_case(case_id, scratch / case_id)
    expected = str(case.manifest.expected_behavior.get("outcome", "success"))
    expected_negative = expected not in {"success", "success_after_repair"}
    shadow = ContentShadow.from_tree(case.origin)
    if expected_negative and case_id == "insufficient_context":
        transport = ScriptedTransport(out_of_policy_plan_script(case.manifest))
    elif expected_negative and case_id == "budget_exhaustion":
        # Tiny limits in the fixture config should refuse before or during plan.
        transport = ScriptedTransport([plan_completion(case.manifest)])
    else:
        transport = ScriptedTransport(success_script(case.manifest, shadow))
    install_gateway_transport(monkeypatch, transport)
    bind_feature_session(monkeypatch, case.origin)
    result = invoke_cli(
        "--config",
        str(case.config_path),
        "feature",
        case.manifest.request,
        "--auto",
    )
    view = parse_view(result.stdout) if result.exit_code == 0 else {}
    feature_id = view.get("feature_id")
    status = view.get("status", "error")
    reason = view.get("reason", "")
    oracle_ok = None
    metrics_path = None
    report = None
    if feature_id:
        worktree = worktree_for(case.origin, feature_id)
        if not expected_negative:
            oracle_ok = all(item.ok for item in run_all_oracles(case.manifest, worktree))
        journal = journal_for(worktree)
        store = JsonCheckpointStore(worktree)
        report = project_metrics(
            feature_id=feature_id,
            journal=journal,
            store=store,
            provenance="mock_functional",
        )
        metrics_path = str(output_dir / f"{case_id}-{feature_id}.metrics.json")
        write_report(Path(metrics_path), report)
    duration_ms = int((time.monotonic() - started) * 1000)
    matched = (
        (expected_negative and status in {"failed", "budget_exhausted", "interrupted"})
        or (not expected_negative and status == "success" and oracle_ok)
    )
    log.info(
        "[Benchmark.run] case=%s mode=offline status=%s matched=%s",
        case_id,
        status,
        matched,
    )
    return CaseOutcome(
        case_id=case_id,
        mode="offline_mock",
        status=status if matched else f"unexpected:{status}",
        reason=reason,
        expected_outcome=expected,
        oracle_ok=oracle_ok,
        expected_negative=expected_negative,
        duration_ms=duration_ms,
        feature_id=feature_id,
        metrics_path=metrics_path,
        error=None if matched else "outcome_mismatch",
        exit_code=result.exit_code,
        settings=describe_settings(case.config_path),
        retry_count=report.workflow_retry_count if report else None,
        verification_status=_verification_status(view),
        check_executions=report.checks.executions if report else None,
        check_passes=report.checks.passes if report else None,
        check_failures=report.checks.failures if report else None,
    )


def run_live_case(
    case_id: str,
    scratch: Path,
    *,
    config_path: Path,
    output_dir: Path,
    deadline_s: float,
) -> CaseOutcome:
    started = time.monotonic()
    validate_live_config(config_path, case_id)
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        raise FixtureError("live_key_missing")
    origin = materialize(case_id, scratch / case_id / "origin").root
    # Private external config snapshot without credentials.
    snapshot = scratch / case_id / "live-operator.yaml"
    snapshot.write_bytes(config_path.read_bytes())
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(scratch / case_id),
        "DEEPSEEK_API_KEY": key,
        "LANG": "C",
        "LC_ALL": "C",
    }
    remaining = max(1.0, deadline_s - (time.monotonic() - started))
    completed = subprocess.run(
        [
            *installed_wl(),
            "--config",
            str(snapshot),
            "feature",
            load_manifest(case_id).request,
            "--auto",
        ],
        cwd=str(origin),
        env=env,
        capture_output=True,
        text=True,
        timeout=remaining,
        check=False,
    )
    view = parse_view(completed.stdout) if completed.returncode == 0 else {}
    feature_id = view.get("feature_id")
    status = view.get("status", "error")
    reason = view.get("reason", "")
    oracle_ok = None
    metrics_path = None
    report = None
    if feature_id:
        worktree = worktree_for(origin, feature_id)
        manifest = load_manifest(case_id)
        oracle_ok = all(item.ok for item in run_all_oracles(manifest, worktree))
        report = project_metrics(
            feature_id=feature_id,
            journal=journal_for(worktree),
            store=JsonCheckpointStore(worktree),
            provenance="deepseek_live",
        )
        metrics_path = str(output_dir / f"live-{case_id}-{feature_id}.metrics.json")
        write_report(Path(metrics_path), report)
    duration_ms = int((time.monotonic() - started) * 1000)
    return CaseOutcome(
        case_id=case_id,
        mode="live_deepseek",
        status=status,
        reason=reason,
        expected_outcome=str(load_manifest(case_id).expected_behavior.get("outcome")),
        oracle_ok=oracle_ok,
        expected_negative=False,
        duration_ms=duration_ms,
        feature_id=feature_id,
        metrics_path=metrics_path,
        error=None if completed.returncode == 0 else "live_command_failed",
        exit_code=completed.returncode,
        settings=describe_settings(snapshot),
        retry_count=report.workflow_retry_count if report else None,
        verification_status=_verification_status(view),
        check_executions=report.checks.executions if report else None,
        check_passes=report.checks.passes if report else None,
        check_failures=report.checks.failures if report else None,
    )


def render_runner_summary(report: RunnerReport) -> str:
    lines = [
        f"Benchmark runner v{report.schema_version} mode={report.mode}",
        f"revision={report.revision}",
        f"cases={len(report.cases)} incomplete={report.incomplete}",
    ]
    if report.live_skipped_reason:
        lines.append(f"live_skipped={report.live_skipped_reason}")
    correct = 0
    for case in report.cases:
        ok = case.error is None
        correct += int(ok)
        lines.append(
            f"case={case.case_id} status={case.status} expected={case.expected_outcome} "
            f"exit={_fmt_optional(case.exit_code)} verification={case.verification_status or 'unavailable'} "
            f"retries={_fmt_optional(case.retry_count)} oracle={case.oracle_ok} "
            f"duration_ms={case.duration_ms}"
        )
    lines.append(f"correctness={correct}/{len(report.cases)}")
    return "\n".join(lines) + "\n"


def _fmt_optional(value: int | None) -> str:
    return "unavailable" if value is None else str(value)


def _verification_status(view: Mapping[str, str]) -> str | None:
    value = view.get("verification", "").strip()
    return value.split(" ", 1)[0] if value else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lean Weave MVP benchmark runner")
    parser.add_argument("--live", action="store_true", help="Enable DeepSeek live mode")
    parser.add_argument("--cases", default="bug_fix", help="Comma-separated fixture ids")
    parser.add_argument("--output", type=Path, default=Path("benchmarks/baselines"))
    parser.add_argument("--live-config", type=Path, default=None)
    parser.add_argument("--aggregate-ceiling", type=int, default=50_000)
    parser.add_argument("--deadline-s", type=float, default=120.0)
    args = parser.parse_args(list(argv) if argv is not None else None)

    selected = [item.strip() for item in args.cases.split(",") if item.strip()]
    for case_id in selected:
        if case_id not in CASE_IDS:
            log.error("[Benchmark.run] unknown_case=%s", case_id)
            return 2

    args.output.mkdir(parents=True, exist_ok=True)
    mode: Mode = "live_deepseek" if args.live else "offline_mock"
    report = RunnerReport(
        schema_version=RUNNER_SCHEMA_VERSION,
        mode=mode,
        revision=_repo_revision(),
    )

    if args.live:
        if args.live_config is None:
            report.live_skipped_reason = "live_config_required"
            report.incomplete = True
        elif not os.environ.get("DEEPSEEK_API_KEY", "").strip():
            report.live_skipped_reason = "live_key_missing"
            report.incomplete = True
        else:
            # Conservative preflight: refuse oversized selections before launch.
            if len(selected) * 5_000 > args.aggregate_ceiling:
                report.live_skipped_reason = "aggregate_ceiling"
                report.incomplete = True
            else:
                scratch = args.output / "_live_scratch"
                scratch.mkdir(parents=True, exist_ok=True)
                for case_id in selected:
                    try:
                        validate_live_config(args.live_config, case_id)
                        outcome = run_live_case(
                            case_id,
                            scratch,
                            config_path=args.live_config,
                            output_dir=args.output,
                            deadline_s=args.deadline_s,
                        )
                        report.cases.append(outcome)
                    except FixtureError as exc:
                        report.incomplete = True
                        report.cases.append(
                            CaseOutcome(
                                case_id=case_id,
                                mode="live_deepseek",
                                status="refused",
                                reason=str(exc),
                                expected_outcome=str(
                                    load_manifest(case_id).expected_behavior.get("outcome")
                                ),
                                oracle_ok=None,
                                expected_negative=False,
                                duration_ms=0,
                                error=str(exc),
                            )
                        )
                        break
        path = args.output / "runner-live.json"
        path.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
        print(render_runner_summary(report), end="")
        return 0 if not report.incomplete else 1

    # Offline path requires pytest monkeypatch; expose library API for tests.
    path = args.output / "runner-offline-note.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": RUNNER_SCHEMA_VERSION,
                "mode": "offline_mock",
                "note": "Use pytest tests/benchmarks/test_run.py for in-process offline evidence",
                "cases": selected,
                "revision": _repo_revision(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(render_runner_summary(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
