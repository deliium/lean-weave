"""Git isolation and frozen-authority regressions through the public CLI."""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from benchmarks.fixtures import intended_edit_files
from benchmarks.offline import (
    ContentShadow,
    ScriptedTransport,
    approval_token,
    bind_feature_session,
    final_completion,
    install_gateway_transport,
    invoke_cli,
    journal_for,
    parse_view,
    plan_completion,
    prepare_case,
    review_plan_only_script,
    success_script,
    tool_completion,
    worktree_for,
    write_operator_config,
)


def _git(origin: Path, *args: str) -> bytes:
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


def _origin_snapshot(origin: Path) -> dict[str, object]:
    tracked = {
        path.relative_to(origin).as_posix(): path.read_bytes()
        for path in origin.rglob("*")
        if path.is_file() and ".git" not in path.parts and ".fixture-home" not in path.parts
    }
    return {
        "tracked": tracked,
        "head": _git(origin, "rev-parse", "HEAD"),
        "branch": _git(origin, "branch", "--show-current"),
        "status": _git(origin, "status", "--porcelain=v1", "-uall"),
        "index": (origin / ".git" / "index").read_bytes(),
    }


def test_dirty_origin_stays_intact_and_worktree_is_clean_baseline(tmp_path: Path, monkeypatch):
    case = prepare_case("bug_fix", tmp_path)
    (case.origin / "src" / "calc.py").write_text("value = dirty\n", encoding="utf-8")
    (case.origin / "staged.py").write_text("staged\n", encoding="utf-8")
    _git(case.origin, "add", "staged.py")
    (case.origin / "untracked.py").write_text("loose\n", encoding="utf-8")
    before = _origin_snapshot(case.origin)

    # Operator config still targets the pinned tree path; materialize a clean
    # shadow from HEAD by re-reading committed bytes for the scripted edit.
    committed = _git(case.origin, "show", "HEAD:src/calc.py").decode("utf-8")
    shadow = ContentShadow(files={"src/calc.py": committed.encode("utf-8")})
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
    assert result.exit_code == 0, result.stdout + result.stderr
    view = parse_view(result.stdout)
    assert view["status"] == "success"
    assert _origin_snapshot(case.origin) == before
    worktree = worktree_for(case.origin, view["feature_id"])
    assert not (worktree / "untracked.py").exists()
    assert not (worktree / "staged.py").exists()
    assert (worktree / "src" / "calc.py").read_text(encoding="utf-8") != "value = dirty\n"
    assert "return a + b" in (worktree / "src" / "calc.py").read_text(encoding="utf-8")
    # No commits/merges/pushes on origin.
    assert _git(case.origin, "rev-parse", "HEAD") == before["head"]
    assert _git(case.origin, "remote").strip() == b""


def test_separate_runs_do_not_share_checkpoints(tmp_path: Path, monkeypatch):
    case = prepare_case("bug_fix", tmp_path)
    before = _origin_snapshot(case.origin)
    for label in ("a", "b"):
        shadow = ContentShadow.from_tree(case.origin)
        transport = ScriptedTransport(success_script(case.manifest, shadow))
        install_gateway_transport(monkeypatch, transport)
        bind_feature_session(monkeypatch, case.origin)
        result = invoke_cli(
            "--config",
            str(case.config_path),
            "feature",
            f"{case.manifest.request} ({label})",
            "--auto",
        )
        assert result.exit_code == 0, result.stdout + result.stderr
    assert _origin_snapshot(case.origin) == before


def test_wrong_review_token_and_mid_plan_fingerprint(tmp_path: Path, monkeypatch):
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
    token = approval_token(paused.stdout)
    wrong = invoke_cli(
        "--config",
        str(case.config_path),
        "resume",
        feature_id,
        "--approve",
        "deadbeef" * 4,
    )
    assert wrong.exit_code == 0, wrong.stdout + wrong.stderr
    assert "invalid_review_token" in wrong.stdout

    worktree = worktree_for(case.origin, feature_id)
    (worktree / "src" / "greeter.py").write_text("TAMPERED\n", encoding="utf-8")
    stale = invoke_cli(
        "--config",
        str(case.config_path),
        "resume",
        feature_id,
        "--approve",
        token,
    )
    assert stale.exit_code == 0, stale.stdout + stale.stderr
    assert "stale_baseline" in stale.stdout


def test_check_definition_and_limit_drift_rejected_before_dispatch(
    tmp_path: Path, monkeypatch
):
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
    token = approval_token(paused.stdout)
    sends_before = transport.send_count

    # Same check id, altered argv (definition drift).
    payload = yaml.safe_load(case.config_path.read_text(encoding="utf-8"))
    payload["tools"]["checks"][0]["argv"].append("--drift")
    drifted = tmp_path / "drifted-check.yaml"
    drifted.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    refused = invoke_cli(
        "--config",
        str(drifted),
        "resume",
        feature_id,
        "--approve",
        token,
    )
    assert refused.exit_code == 0, refused.stdout + refused.stderr
    assert "authority_drift" in refused.stdout or "stale_binding" in refused.stdout
    assert transport.send_count == sends_before

    # Limit drift with an otherwise identical check body.
    payload2 = yaml.safe_load(case.config_path.read_text(encoding="utf-8"))
    payload2.setdefault("workflow", {})
    payload2["workflow"]["max_retries"] = 99
    limit_drift = tmp_path / "drifted-limit.yaml"
    limit_drift.write_text(yaml.safe_dump(payload2, sort_keys=False), encoding="utf-8")
    refused_limit = invoke_cli(
        "--config",
        str(limit_drift),
        "resume",
        feature_id,
        "--approve",
        token,
    )
    assert refused_limit.exit_code == 0, refused_limit.stdout + refused_limit.stderr
    assert (
        "authority_drift" in refused_limit.stdout
        or "stale_binding" in refused_limit.stdout
    )
    assert transport.send_count == sends_before


def test_state_dir_drift_refuses_and_preserves_journal(tmp_path: Path, monkeypatch):
    case = prepare_case("bug_fix", tmp_path)
    transport = ScriptedTransport(success_script(case.manifest, case.shadow))
    install_gateway_transport(monkeypatch, transport)
    bind_feature_session(monkeypatch, case.origin)
    # Interrupt after plan via review so resume is required.
    review_transport = ScriptedTransport(review_plan_only_script(case.manifest))
    install_gateway_transport(monkeypatch, review_transport)
    paused = invoke_cli(
        "--config",
        str(case.config_path),
        "feature",
        case.manifest.request,
        "--review",
    )
    feature_id = parse_view(paused.stdout)["feature_id"]
    worktree = worktree_for(case.origin, feature_id)
    journal_path = journal_for(worktree).journal_path
    before = journal_path.read_bytes() if journal_path.exists() else b""

    payload = yaml.safe_load(case.config_path.read_text(encoding="utf-8"))
    payload.setdefault("workflow", {})
    payload["workflow"]["state_dir"] = ".lean-weave-other"
    drifted = tmp_path / "state-dir.yaml"
    drifted.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    token = approval_token(paused.stdout)
    refused = invoke_cli(
        "--config",
        str(drifted),
        "resume",
        feature_id,
        "--approve",
        token,
    )
    assert refused.exit_code == 3
    assert "state_dir_drift" in refused.stdout + refused.stderr
    after = journal_path.read_bytes() if journal_path.exists() else b""
    assert after == before
    assert not (worktree / ".lean-weave-other").exists()


def test_workspace_code_check_without_backend_refused(tmp_path: Path, monkeypatch):
    case = prepare_case("bug_fix", tmp_path)
    payload = yaml.safe_load(case.config_path.read_text(encoding="utf-8"))
    payload["tools"]["checks"][0]["runs_workspace_code"] = True
    config = tmp_path / "ws-code.yaml"
    config.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    transport = ScriptedTransport([plan_completion(case.manifest)])
    install_gateway_transport(monkeypatch, transport)
    bind_feature_session(monkeypatch, case.origin)
    result = invoke_cli(
        "--config",
        str(config),
        "feature",
        case.manifest.request,
        "--auto",
    )
    assert result.exit_code == 3
    assert "workspace_code_backend_required" in result.stdout + result.stderr
    assert transport.send_count == 0


def test_unauthorized_write_path_fails_closed(tmp_path: Path, monkeypatch):
    case = prepare_case("bug_fix", tmp_path, write_allow=["src/other.py"])
    # write_allow for a missing path should fail during approval enumeration.
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
    assert result.exit_code != 0
    assert transport.send_count == 0
