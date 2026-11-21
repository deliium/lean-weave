"""Offline public CLI path with real DeepSeek gateway accounting (mocked HTTP)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from benchmarks.fixtures import apply_intended_edit, run_all_oracles
from benchmarks.offline import (
    ContentShadow,
    ScriptedTransport,
    approval_token,
    assert_shared_journal,
    bad_dependency_plan_script,
    bind_feature_session,
    capture_orchestrators,
    final_completion,
    install_gateway_transport,
    invoke_cli,
    journal_for,
    out_of_policy_plan_script,
    parse_view,
    prepare_case,
    review_plan_only_script,
    run_installed,
    success_script,
    tool_completion,
    worktree_for,
)
from lean_weave.config import init_config


def _snapshot_origin(origin: Path) -> dict[str, object]:
    def run(*args: str) -> bytes:
        return subprocess.run(
            ["git", "-C", str(origin), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(origin),
                "GIT_CONFIG_NOSYSTEM": "1",
            },
        ).stdout

    tracked = {
        path.relative_to(origin).as_posix(): path.read_bytes()
        for path in origin.rglob("*")
        if path.is_file() and ".git" not in path.parts and ".fixture-home" not in path.parts
    }
    return {
        "tracked": tracked,
        "head": run("rev-parse", "HEAD"),
        "status": run("status", "--porcelain=v1", "-uall"),
        "index": (origin / ".git" / "index").read_bytes(),
    }


def test_init_and_doctor_with_installed_wl_offline(tmp_path: Path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    init = run_installed("init", cwd=cwd)
    assert init.returncode == 0, init.stdout + init.stderr
    config = Path(init.stdout.strip())
    assert config.is_file()
    doctor = run_installed("--config", str(config), "doctor", cwd=cwd)
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    assert "DEEPSEEK" in doctor.stdout.upper() or "api" in doctor.stdout.lower() or doctor.stdout


def test_repo_defaults_alone_cannot_authorize_fixture_checks(tmp_path: Path):
    case = prepare_case("bug_fix", tmp_path)
    default_config = init_config(tmp_path / "defaults.yaml", force=True)
    text = default_config.read_text(encoding="utf-8")
    assert "assert_add" not in text
    assert case.manifest.check_ids[0] not in text


def test_auto_edits_verifies_and_settles_shared_journal(tmp_path: Path, monkeypatch):
    case = prepare_case("bug_fix", tmp_path)
    before = _snapshot_origin(case.origin)
    transport = ScriptedTransport(success_script(case.manifest, case.shadow))
    providers = install_gateway_transport(monkeypatch, transport)
    orchestrators = capture_orchestrators(monkeypatch)
    bind_feature_session(monkeypatch, case.origin)

    result = invoke_cli(
        "--config",
        str(case.config_path),
        "feature",
        case.manifest.request,
        "--auto",
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    view = parse_view(result.stdout)
    assert view["status"] == "success"
    assert view["stage"] == "done"
    assert "verification=pass" in result.stdout
    assert "usage=" in result.stdout
    assert _snapshot_origin(case.origin) == before

    worktree = worktree_for(case.origin, view["feature_id"])
    assert "return a + b" in (worktree / "src" / "calc.py").read_text(encoding="utf-8")
    assert all(item.ok for item in run_all_oracles(case.manifest, worktree))

    live = assert_shared_journal(providers, orchestrators)
    # Reopen from disk and correlate without treating values as live API spend.
    reopened = journal_for(worktree)
    assert reopened.journal_path == live.journal_path
    spent = reopened.spent(view["feature_id"])
    assert spent > 0
    assert transport.send_count >= 3
    assert reopened.totals(view["feature_id"]).total_tokens == spent


def test_review_persists_plan_without_patch_until_approve(tmp_path: Path, monkeypatch):
    case = prepare_case("single_file_feature", tmp_path)
    transport = ScriptedTransport(review_plan_only_script(case.manifest))
    install_gateway_transport(monkeypatch, transport)
    bind_feature_session(monkeypatch, case.origin)

    paused = invoke_cli(
        "--config",
        str(case.config_path),
        "feature",
        case.manifest.request,
        "--review",
    )
    assert paused.exit_code == 0, paused.stdout + paused.stderr
    view = parse_view(paused.stdout)
    assert view["reason"] == "review_pending"
    assert "plan_goal=" in paused.stdout
    feature_id = view["feature_id"]
    worktree = worktree_for(case.origin, feature_id)
    assert (worktree / "src" / "greeter.py").read_text(encoding="utf-8") == (
        case.origin / "src" / "greeter.py"
    ).read_text(encoding="utf-8")
    assert transport.send_count == 1

    # Continue with remaining implement script on a fresh transport injection.
    shadow = ContentShadow.from_tree(worktree)
    edits = {"src/greeter.py": 'def greet(name):\n    return f"Hello, {name}"\n'}
    continue_transport = ScriptedTransport(
        [
            tool_completion(shadow, "src/greeter.py", edits["src/greeter.py"]),
            final_completion("done"),
        ]
    )
    install_gateway_transport(monkeypatch, continue_transport)
    token = approval_token(paused.stdout)
    done = invoke_cli(
        "--config",
        str(case.config_path),
        "resume",
        feature_id,
        "--approve",
        token,
    )
    assert done.exit_code == 0, done.stdout + done.stderr
    assert parse_view(done.stdout)["status"] == "success"
    assert continue_transport.send_count == 2
    assert "Hello" in (worktree / "src" / "greeter.py").read_text(encoding="utf-8")


def test_plan_rejection_out_of_policy_and_bad_dependency(tmp_path: Path, monkeypatch):
    case = prepare_case("insufficient_context", tmp_path)
    transport = ScriptedTransport(out_of_policy_plan_script(case.manifest))
    install_gateway_transport(monkeypatch, transport)
    bind_feature_session(monkeypatch, case.origin)
    rejected = invoke_cli(
        "--config",
        str(case.config_path),
        "feature",
        case.manifest.request,
        "--auto",
    )
    assert rejected.exit_code == 0, rejected.stdout + rejected.stderr
    view = parse_view(rejected.stdout)
    assert view["status"] == "failed"
    assert (case.origin / "src" / "visible.py").read_text(encoding="utf-8").count(
        "unset"
    ) == 1

    case2 = prepare_case("bug_fix", tmp_path / "dep")
    transport2 = ScriptedTransport(bad_dependency_plan_script(case2.manifest))
    install_gateway_transport(monkeypatch, transport2)
    bind_feature_session(monkeypatch, case2.origin)
    bad_dep = invoke_cli(
        "--config",
        str(case2.config_path),
        "feature",
        case2.manifest.request,
        "--auto",
    )
    assert bad_dep.exit_code == 0, bad_dep.stdout + bad_dep.stderr
    assert parse_view(bad_dep.stdout)["status"] == "failed"


def test_multi_file_ordered_tasks_then_one_verification(tmp_path: Path, monkeypatch):
    case = prepare_case("multi_file_feature", tmp_path)
    transport = ScriptedTransport(success_script(case.manifest, case.shadow))
    install_gateway_transport(monkeypatch, transport)
    bind_feature_session(monkeypatch, case.origin)
    result = invoke_cli(
        "--config",
        str(case.config_path),
        "feature",
        case.manifest.request,
        "--auto",
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    view = parse_view(result.stdout)
    assert view["status"] == "success"
    worktree = worktree_for(case.origin, view["feature_id"])
    assert 'VERSION = "1.0.0"' in (worktree / "src" / "config.py").read_text(
        encoding="utf-8"
    )
    assert "v" in (worktree / "src" / "app.py").read_text(encoding="utf-8")
    # Plan + (tool+final) * 2 tasks
    assert transport.send_count == 5
    assert "verification=pass" in result.stdout


def test_status_and_result_without_config_preserve_checkpoint(tmp_path: Path, monkeypatch):
    case = prepare_case("bug_fix", tmp_path)
    transport = ScriptedTransport(success_script(case.manifest, case.shadow))
    install_gateway_transport(monkeypatch, transport)
    bind_feature_session(monkeypatch, case.origin)
    started = invoke_cli(
        "--config",
        str(case.config_path),
        "feature",
        case.manifest.request,
        "--auto",
    )
    feature_id = parse_view(started.stdout)["feature_id"]
    worktree = worktree_for(case.origin, feature_id)
    journal_path = journal_for(worktree).journal_path
    before_journal = journal_path.read_bytes()
    checkpoint = next((worktree / ".lean-weave" / "workflow").glob("*.json"))
    before_checkpoint = checkpoint.read_bytes()

    status = run_installed("status", feature_id, cwd=case.origin)
    assert status.returncode == 0, status.stdout + status.stderr
    assert f"feature_id={feature_id}" in status.stdout
    assert "status=success" in status.stdout

    outcome = run_installed("result", feature_id, cwd=case.origin)
    assert outcome.returncode == 0, outcome.stdout + outcome.stderr
    assert "status=success" in outcome.stdout
    assert journal_path.read_bytes() == before_journal
    assert checkpoint.read_bytes() == before_checkpoint


def test_invalid_mode_combination_exits_config(tmp_path: Path, monkeypatch):
    case = prepare_case("bug_fix", tmp_path)
    bind_feature_session(monkeypatch, case.origin)
    both = invoke_cli(
        "--config",
        str(case.config_path),
        "feature",
        "x",
        "--auto",
        "--review",
    )
    assert both.exit_code == 3
    neither = invoke_cli("--config", str(case.config_path), "feature", "x")
    assert neither.exit_code == 3


def test_feature_preflight_smoke_without_network(tmp_path: Path):
    case = prepare_case("bug_fix", tmp_path)
    # No key and no transport: executable must fail closed without dialing out.
    # Public feature commands still exit 0 when they render a typed interruption.
    smoke = run_installed(
        "--config",
        str(case.config_path),
        "feature",
        case.manifest.request,
        "--auto",
        cwd=case.origin,
        env={"DEEPSEEK_API_KEY": ""},
    )
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr
    combined = smoke.stdout + smoke.stderr
    assert "provider_error" in combined or "status=interrupted" in combined
    assert "return a + b" not in (case.origin / "src" / "calc.py").read_text(
        encoding="utf-8"
    )


def test_intended_edit_still_satisfies_oracle_outside_cli(tmp_path: Path):
    case = prepare_case("test_repair", tmp_path)
    apply_intended_edit(case.manifest.id, case.origin)
    assert all(item.ok for item in run_all_oracles(case.manifest, case.origin))
