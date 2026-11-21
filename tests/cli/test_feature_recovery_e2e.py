"""Cross-layer retry, budget, cancel, and recovery evidence through the public CLI."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest
import yaml

import lean_weave.workflow.tools as tools_module
from benchmarks.fixtures import intended_edit_files, load_manifest, run_oracle
from benchmarks.offline import (
    ContentShadow,
    ScriptedTransport,
    assert_shared_journal,
    bind_feature_session,
    capture_orchestrators,
    chat_completion,
    final_completion,
    install_gateway_transport,
    invoke_cli,
    journal_for,
    out_of_policy_plan_script,
    parse_view,
    plan_completion,
    prepare_case,
    review_plan_only_script,
    success_script,
    tool_completion,
    worktree_for,
    write_operator_config,
)
from lean_weave.worktree.git import preflight_repository
from lean_weave.worktree.registry import feature_routing_path, routing_root


def _broken_marker() -> str:
    return "VALUE =\n"


def _fixed_marker() -> str:
    return "VALUE = 2\n"


def test_repair_after_recognized_diagnostic_then_pass(tmp_path: Path, monkeypatch):
    case = prepare_case("verify_then_repair", tmp_path)
    shadow = case.shadow
    transport = ScriptedTransport(
        [
            plan_completion(case.manifest),
            tool_completion(shadow, "src/marker.py", _broken_marker(), call_id="c1"),
            final_completion("broke"),
            tool_completion(shadow, "src/marker.py", _fixed_marker(), call_id="c2"),
            final_completion("repaired"),
        ]
    )
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
    assert (worktree / "src" / "marker.py").read_text(encoding="utf-8") == _fixed_marker()
    assert run_oracle(case.manifest, worktree, "assert_marker").ok
    # plan + bad implement(tool,final) + retry(tool,final) = 5 sends; verify is local
    assert transport.send_count == 5


def test_generic_check_failure_does_not_retry(tmp_path: Path, monkeypatch):
    case = prepare_case("bug_fix", tmp_path)
    generic = tmp_path / "generic_fail.py"
    generic.write_text(
        "import sys\nprint('boom')\nraise SystemExit(1)\n",
        encoding="utf-8",
    )
    # Rewrite operator config with a non-attributable failing check.
    manifest = load_manifest("bug_fix")
    config = write_operator_config(tmp_path / "generic.yaml", manifest)
    payload = yaml.safe_load(config.read_text(encoding="utf-8"))
    payload["tools"]["checks"] = [
        {
            "id": "assert_add",
            "executable": sys.executable,
            "argv": [sys.executable, str(generic)],
            "timeout_s": 5,
            "output_bytes_max": 65536,
            "runs_workspace_code": False,
        }
    ]
    config.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    shadow = ContentShadow.from_tree(case.origin)
    # Apply a "successful" edit so the model thinks it is done; check still fails generically.
    transport = ScriptedTransport(
        [
            plan_completion(manifest),
            tool_completion(
                shadow,
                "src/calc.py",
                intended_edit_files("bug_fix")["src/calc.py"],
            ),
            final_completion("done"),
            # No retry responses — a retry would exhaust the script.
        ]
    )
    install_gateway_transport(monkeypatch, transport)
    bind_feature_session(monkeypatch, case.origin)
    result = invoke_cli(
        "--config",
        str(config),
        "feature",
        manifest.request,
        "--auto",
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    view = parse_view(result.stdout)
    assert view["status"] == "failed"
    assert view["reason"] in {"verification_failed", "ambiguous_diagnostics"}
    assert transport.send_count == 3


def test_retry_limit_zero_and_exhausted(tmp_path: Path, monkeypatch):
    case = prepare_case(
        "verify_then_repair",
        tmp_path / "zero",
        extra_workflow={"max_retries": 0},
    )
    shadow = case.shadow
    transport = ScriptedTransport(
        [
            plan_completion(case.manifest),
            tool_completion(shadow, "src/marker.py", _broken_marker()),
            final_completion("broke"),
        ]
    )
    install_gateway_transport(monkeypatch, transport)
    bind_feature_session(monkeypatch, case.origin)
    zero = invoke_cli(
        "--config",
        str(case.config_path),
        "feature",
        case.manifest.request,
        "--auto",
    )
    assert zero.exit_code == 0, zero.stdout + zero.stderr
    assert parse_view(zero.stdout)["reason"] == "retry_exhausted"

    case2 = prepare_case(
        "verify_then_repair",
        tmp_path / "one",
        extra_workflow={"max_retries": 1},
    )
    shadow2 = case2.shadow
    transport2 = ScriptedTransport(
        [
            plan_completion(case2.manifest),
            tool_completion(shadow2, "src/marker.py", _broken_marker(), call_id="c1"),
            final_completion("broke"),
            final_completion("still broken"),  # retry without a fixing patch
        ]
    )
    install_gateway_transport(monkeypatch, transport2)
    bind_feature_session(monkeypatch, case2.origin)
    exhausted = invoke_cli(
        "--config",
        str(case2.config_path),
        "feature",
        case2.manifest.request,
        "--auto",
    )
    assert exhausted.exit_code == 0, exhausted.stdout + exhausted.stderr
    view = parse_view(exhausted.stdout)
    assert view["status"] == "failed"
    assert view["reason"] == "retry_exhausted"


def test_budget_exhaustion_before_dispatch_and_across_resume(tmp_path: Path, monkeypatch):
    case = prepare_case(
        "budget_exhaustion",
        tmp_path,
        extra_workflow={"max_task_tokens": 1, "feature_token_budget": 1},
    )
    transport = ScriptedTransport([plan_completion(case.manifest)])
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
    assert view["status"] == "budget_exhausted"
    # Plan may be denied before any send when reservation cannot fit.
    feature_id = view["feature_id"]
    worktree = worktree_for(case.origin, feature_id)
    assert (worktree / "src" / "small.py").read_text(encoding="utf-8") == (
        case.origin / "src" / "small.py"
    ).read_text(encoding="utf-8")

    resumed = invoke_cli("--config", str(case.config_path), "resume", feature_id)
    assert resumed.exit_code == 0, resumed.stdout + resumed.stderr
    assert parse_view(resumed.stdout)["status"] == "budget_exhausted"
    assert transport.send_count <= 1


def test_context_insufficient_plan_rejection_no_widen(tmp_path: Path, monkeypatch):
    case = prepare_case("insufficient_context", tmp_path)
    transport = ScriptedTransport(out_of_policy_plan_script(case.manifest))
    install_gateway_transport(monkeypatch, transport)
    bind_feature_session(monkeypatch, case.origin)
    result = invoke_cli(
        "--config",
        str(case.config_path),
        "feature",
        case.manifest.request,
        "--auto",
    )
    view = parse_view(result.stdout)
    assert view["status"] == "failed"
    worktree = worktree_for(case.origin, view["feature_id"])
    assert (worktree / "private" / "needed.py").read_text(encoding="utf-8") == (
        case.origin / "private" / "needed.py"
    ).read_text(encoding="utf-8")
    assert 'FLAG = "unset"' in (worktree / "src" / "visible.py").read_text(encoding="utf-8")


def test_resume_retains_spend_after_provider_interrupt(tmp_path: Path, monkeypatch):
    case = prepare_case("bug_fix", tmp_path)
    shadow = case.shadow

    # Plan succeeds, implement fails once, resume patches.
    class FlakyTransport(ScriptedTransport):
        def handler(self, request):
            if len(self.requests) == 1:
                # Second logical call: fail closed at HTTP layer.
                body = __import__("json").loads(request.content.decode("utf-8"))
                self.requests.append(body)
                return __import__("httpx2").Response(
                    500, json={"error": {"message": "transient"}}
                )
            return super().handler(request)

    steps = [
        plan_completion(case.manifest),
        # placeholder never used due to 500 on second send
        final_completion("unused"),
    ]
    transport = FlakyTransport(steps)
    providers = install_gateway_transport(monkeypatch, transport)
    orchestrators = capture_orchestrators(monkeypatch)
    bind_feature_session(monkeypatch, case.origin)
    started = invoke_cli(
        "--config",
        str(case.config_path),
        "feature",
        case.manifest.request,
        "--auto",
    )
    assert started.exit_code == 0, started.stdout + started.stderr
    view = parse_view(started.stdout)
    assert view["status"] in {"interrupted", "failed"}
    feature_id = view["feature_id"]
    worktree = worktree_for(case.origin, feature_id)
    live = assert_shared_journal(providers, orchestrators)
    spent_before = live.spent(feature_id)
    assert spent_before > 0

    resume_shadow = ContentShadow.from_tree(worktree)
    resume_transport = ScriptedTransport(
        [
            tool_completion(
                resume_shadow,
                "src/calc.py",
                intended_edit_files("bug_fix")["src/calc.py"],
            ),
            final_completion("done"),
        ]
    )
    install_gateway_transport(monkeypatch, resume_transport)
    resumed = invoke_cli("--config", str(case.config_path), "resume", feature_id)
    assert resumed.exit_code == 0, resumed.stdout + resumed.stderr
    assert parse_view(resumed.stdout)["status"] == "success"
    reopened = journal_for(worktree)
    assert reopened.spent(feature_id) >= spent_before


def test_committed_unconfirmed_requires_manual_inspection(tmp_path: Path, monkeypatch):
    case = prepare_case("bug_fix", tmp_path)
    shadow = case.shadow

    transport = ScriptedTransport(
        [
            plan_completion(case.manifest),
            tool_completion(
                shadow,
                "src/calc.py",
                intended_edit_files("bug_fix")["src/calc.py"],
            ),
        ]
    )
    original = tools_module.apply_patch

    def unconfirmed(approval, request):
        result = original(approval, request)
        if hasattr(result, "durability"):
            return result.model_copy(update={"durability": "unconfirmed"})
        return result

    monkeypatch.setattr(tools_module, "apply_patch", unconfirmed)
    install_gateway_transport(monkeypatch, transport)
    bind_feature_session(monkeypatch, case.origin)
    started = invoke_cli(
        "--config",
        str(case.config_path),
        "feature",
        case.manifest.request,
        "--auto",
    )
    assert started.exit_code == 0, started.stdout + started.stderr
    view = parse_view(started.stdout)
    assert view["status"] == "interrupted"
    assert "unconfirmed" in view["reason"] or "unresolved" in view.get("reason", "")
    feature_id = view["feature_id"]
    worktree = worktree_for(case.origin, feature_id)
    assert "return a + b" in (worktree / "src" / "calc.py").read_text(encoding="utf-8")

    resume_transport = ScriptedTransport([])
    install_gateway_transport(monkeypatch, resume_transport)
    again = invoke_cli("--config", str(case.config_path), "resume", feature_id)
    assert again.exit_code == 0, again.stdout + again.stderr
    again_view = parse_view(again.stdout)
    assert again_view["status"] in {"interrupted", "failed"}
    assert resume_transport.send_count == 0
    assert "return a + b" in (worktree / "src" / "calc.py").read_text(encoding="utf-8")


def test_idle_cancel_and_active_cooperative_cancel(tmp_path: Path, monkeypatch):
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
    feature_id = parse_view(paused.stdout)["feature_id"]
    cancelled = invoke_cli("cancel", feature_id)
    assert cancelled.exit_code == 0, cancelled.stdout + cancelled.stderr
    assert parse_view(cancelled.stdout)["status"] == "cancelled"
    terminal = invoke_cli("resume", feature_id)
    assert parse_view(terminal.stdout)["status"] == "cancelled"

    # Active cooperative cancel: block inside implement, cancel from another thread.
    case2 = prepare_case("bug_fix", tmp_path / "active")
    entered = threading.Event()
    release = threading.Event()
    shadow = case2.shadow

    def blocking_tool(body):
        del body
        entered.set()
        assert release.wait(timeout=5)
        return tool_completion(
            shadow,
            "src/calc.py",
            intended_edit_files("bug_fix")["src/calc.py"],
        )

    active_transport = ScriptedTransport(
        [plan_completion(case2.manifest), blocking_tool, final_completion("done")]
    )
    install_gateway_transport(monkeypatch, active_transport)
    bind_feature_session(monkeypatch, case2.origin)
    outcome: dict[str, object] = {}

    def run_feature():
        outcome["result"] = invoke_cli(
            "--config",
            str(case2.config_path),
            "feature",
            case2.manifest.request,
            "--auto",
        )

    worker = threading.Thread(target=run_feature, daemon=True)
    worker.start()
    assert entered.wait(timeout=5)

    identity = preflight_repository(case2.origin)
    features_dir = routing_root(identity.common_dir) / "features"
    feature_ids = sorted(path.stem for path in features_dir.glob("*.json"))
    assert feature_ids, list(features_dir.iterdir()) if features_dir.exists() else features_dir
    active_id = feature_ids[-1]
    assert feature_routing_path(identity.common_dir, active_id).is_file()
    cancel_view = invoke_cli("cancel", active_id)
    assert cancel_view.exit_code == 0, cancel_view.stdout + cancel_view.stderr
    release.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    result = outcome["result"]
    assert result.exit_code == 0, result.stdout + result.stderr
    status = parse_view(result.stdout)["status"]
    # Cancel wins when honored before further effects; otherwise the scripted
    # patch may still finish after the marker is observed on a later round.
    assert status in {"cancelled", "success"}


def test_cleanup_refuses_edited_worktree(tmp_path: Path, monkeypatch):
    case = prepare_case("bug_fix", tmp_path)
    transport = ScriptedTransport(success_script(case.manifest, case.shadow))
    install_gateway_transport(monkeypatch, transport)
    bind_feature_session(monkeypatch, case.origin)
    done = invoke_cli(
        "--config",
        str(case.config_path),
        "feature",
        case.manifest.request,
        "--auto",
    )
    feature_id = parse_view(done.stdout)["feature_id"]
    worktree = worktree_for(case.origin, feature_id)
    before = (worktree / "src" / "calc.py").read_bytes()
    cleaned = invoke_cli("worktree", "cleanup", feature_id, "--confirm")
    assert cleaned.exit_code != 0
    assert "cleanup_dirty" in cleaned.stdout + cleaned.stderr or cleaned.exit_code == 1
    assert (worktree / "src" / "calc.py").read_bytes() == before


def test_terminal_resume_is_idempotent(tmp_path: Path, monkeypatch):
    case = prepare_case("bug_fix", tmp_path)
    transport = ScriptedTransport(success_script(case.manifest, case.shadow))
    install_gateway_transport(monkeypatch, transport)
    bind_feature_session(monkeypatch, case.origin)
    first = invoke_cli(
        "--config",
        str(case.config_path),
        "feature",
        case.manifest.request,
        "--auto",
    )
    feature_id = parse_view(first.stdout)["feature_id"]
    worktree = worktree_for(case.origin, feature_id)
    journal_bytes = journal_for(worktree).journal_path.read_bytes()
    empty = ScriptedTransport([])
    install_gateway_transport(monkeypatch, empty)
    second = invoke_cli("resume", feature_id)
    third = invoke_cli("resume", feature_id)
    assert parse_view(second.stdout)["status"] == "success"
    assert parse_view(third.stdout)["status"] == "success"
    assert empty.send_count == 0
    assert journal_for(worktree).journal_path.read_bytes() == journal_bytes
