"""Public CLI feature flow with explicit V2 workflow context."""

from __future__ import annotations

from pathlib import Path

from benchmarks.fixtures import run_all_oracles
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
    prepare_case,
    success_script,
    tool_completion,
    worktree_for,
)


def _run_v2_success(case, monkeypatch):
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
    return transport, result


def test_v2_multi_file_feature_matches_v1_oracles_and_call_shape(tmp_path: Path, monkeypatch):
    case = prepare_case("multi_file_feature", tmp_path, context_mode="v2")
    transport, result = _run_v2_success(case, monkeypatch)
    assert result.exit_code == 0, result.stdout + result.stderr
    view = parse_view(result.stdout)
    assert view["status"] == "success"
    worktree = worktree_for(case.origin, view["feature_id"])
    assert all(item.ok for item in run_all_oracles(case.manifest, worktree))
    assert transport.send_count == 5
    assert journal_for(worktree).spent(view["feature_id"]) > 0
    # No verification or diagnosis model calls.
    assert all(request.get("stage") != "verify" for request in transport.requests)
    # The first patch in each task uses the revision exposed by the request,
    # not an extra read round.
    assert not any(
        call.get("function", {}).get("name") in {"read_range", "read_symbol"}
        for request in transport.requests
        for message in request.get("messages", [])
        if message.get("role") == "assistant"
        for call in (message.get("tool_calls") or [])
    )


def test_v2_review_pause_and_resume_does_not_replay_planning(tmp_path: Path, monkeypatch):
    from benchmarks.offline import review_plan_only_script, tool_completion

    case = prepare_case("single_file_feature", tmp_path, context_mode="v2")
    plan_transport = ScriptedTransport(review_plan_only_script(case.manifest))
    install_gateway_transport(monkeypatch, plan_transport)
    bind_feature_session(monkeypatch, case.origin)
    paused = invoke_cli(
        "--config", str(case.config_path), "feature", case.manifest.request, "--review"
    )
    assert paused.exit_code == 0, paused.stdout + paused.stderr
    view = parse_view(paused.stdout)
    assert view["reason"] == "review_pending"
    assert plan_transport.send_count == 1
    worktree = worktree_for(case.origin, view["feature_id"])
    before = (worktree / "src/greeter.py").read_text(encoding="utf-8")

    shadow = ContentShadow.from_tree(worktree)
    new_text = 'def greet(name):\n    return f"Hello, {name}"\n'
    resume_transport = ScriptedTransport(
        [
            tool_completion(shadow, "src/greeter.py", new_text),
            final_completion("done"),
        ]
    )
    install_gateway_transport(monkeypatch, resume_transport)
    done = invoke_cli(
        "--config",
        str(case.config_path),
        "resume",
        view["feature_id"],
        "--approve",
        approval_token(paused.stdout),
    )
    assert done.exit_code == 0, done.stdout + done.stderr
    assert parse_view(done.stdout)["status"] == "success"
    assert resume_transport.send_count == 2
    assert "Hello" in (worktree / "src/greeter.py").read_text(encoding="utf-8")
    assert before != new_text


def test_v2_stale_approval_is_rejected_after_approved_file_edit(tmp_path: Path, monkeypatch):
    from benchmarks.offline import review_plan_only_script

    case = prepare_case("single_file_feature", tmp_path, context_mode="v2")
    transport = ScriptedTransport(review_plan_only_script(case.manifest))
    install_gateway_transport(monkeypatch, transport)
    bind_feature_session(monkeypatch, case.origin)
    paused = invoke_cli(
        "--config", str(case.config_path), "feature", case.manifest.request, "--review"
    )
    view = parse_view(paused.stdout)
    worktree = worktree_for(case.origin, view["feature_id"])
    (worktree / "src/greeter.py").write_text(
        "def greet(name):\n    return 'tampered'\n", encoding="utf-8"
    )
    stale = invoke_cli(
        "--config",
        str(case.config_path),
        "resume",
        view["feature_id"],
        "--approve",
        approval_token(paused.stdout),
    )
    assert stale.exit_code == 0, stale.stdout + stale.stderr
    parsed = parse_view(stale.stdout)
    assert parsed["status"] == "interrupted"
    assert parsed["reason"] in {"stale_baseline", "stale_binding"}
    assert transport.send_count == 1
    assert "tampered" in (worktree / "src/greeter.py").read_text(encoding="utf-8")
