"""Feature-worktree cache checks with the existing offline gateway driver."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lean_weave.app import (
    build_app_context,
    create_context_cache,
    create_context_provider,
)
from lean_weave.schemas import Stage
from lean_weave.workflow.state import JsonCheckpointStore

from benchmarks.offline import (
    ContentShadow,
    ScriptedTransport,
    bind_feature_session,
    install_gateway_transport,
    invoke_cli,
    parse_view,
    prepare_case,
    success_script,
    worktree_for,
)


def _enable_cache(config_path: Path) -> None:
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "\ncontext:\n  cache:\n    enabled: true\n",
        encoding="utf-8",
    )


def _run_case(case, monkeypatch) -> tuple[str, Path, int]:
    shadow = ContentShadow.from_tree(case.origin)
    transport = ScriptedTransport(success_script(case.manifest, shadow))
    install_gateway_transport(monkeypatch, transport)
    bind_feature_session(monkeypatch, case.origin)
    result = invoke_cli(
        "--config", str(case.config_path), "feature", case.manifest.request, "--auto"
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    view = parse_view(result.stdout)
    assert view["status"] == "success"
    worktree = worktree_for(case.origin, view["feature_id"])
    return view["feature_id"], worktree, transport.send_count


def test_cache_reuses_same_worktree_and_keeps_gateway_calls(tmp_path: Path, monkeypatch):
    case = prepare_case("bug_fix", tmp_path)
    _enable_cache(case.config_path)

    feature_id, worktree, first_calls = _run_case(case, monkeypatch)
    assert first_calls > 0

    context = build_app_context(case.config_path)
    cache = create_context_cache(context, workspace_root=worktree)
    before_hits = cache.stats().count("hit")

    store = JsonCheckpointStore(worktree, state_dir=context.config.workflow.state_dir)
    envelope = store.load_checkpoint(feature_id)
    plan = envelope.state.plan
    assert plan is not None and plan.tasks
    state = envelope.state.model_copy(
        update={
            "stage": Stage.IMPLEMENT,
            "active_task_id": plan.tasks[0].id,
        }
    )

    def fail_provider(*_args, **_kwargs):
        raise AssertionError("cache warmup must not construct an LLM provider")

    monkeypatch.setattr("lean_weave.app.create_llm_provider", fail_provider)
    enabled = create_context_provider(context, workspace_root=worktree)
    first = enabled.build("implement", state)
    second = create_context_provider(context, workspace_root=worktree).build(
        "implement", state
    )
    assert first["messages"] == second["messages"]
    assert cache.stats().count("hit") > before_hits

    disabled_context = build_app_context(case.config_path)
    disabled_context.config.context.cache.enabled = False
    disabled = create_context_provider(
        disabled_context, workspace_root=worktree
    ).build("implement", state)
    assert disabled["messages"] == first["messages"]


def test_second_checkout_keeps_gateway_calls_and_isolates_namespace(
    tmp_path: Path, monkeypatch
):
    case = prepare_case("bug_fix", tmp_path)
    _enable_cache(case.config_path)

    first_id, first_worktree, first_calls = _run_case(case, monkeypatch)
    second_id, second_worktree, second_calls = _run_case(case, monkeypatch)
    assert first_id != second_id
    assert first_worktree != second_worktree
    assert second_calls == first_calls

    context = build_app_context(case.config_path)
    first_cache = create_context_cache(context, workspace_root=first_worktree)
    second_cache = create_context_cache(context, workspace_root=second_worktree)
    assert first_cache._location().checkout_key != second_cache._location().checkout_key
    assert second_cache.status().health.value == "ready"
    assert context.config.context.cache.enabled is True


def _run_multi_file_v2_review_index_flow(tmp_path, monkeypatch):
    import yaml

    from lean_weave.benchmark.fixtures import intended_edit_files
    from lean_weave.benchmark.results import project_metrics
    from lean_weave.code_index import CodeIndex
    from benchmarks.offline import (
        approval_token,
        final_completion,
        journal_for,
        review_plan_only_script,
        run_installed,
        tool_completion,
    )

    case = prepare_case("multi_file_feature", tmp_path, context_mode="v2")
    payload = yaml.safe_load(case.config_path.read_text(encoding="utf-8"))
    context_section = dict(payload.get("context") or {})
    context_section["cache"] = {"enabled": True}
    payload["context"] = context_section
    case.config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    plan_transport = ScriptedTransport(review_plan_only_script(case.manifest))
    install_gateway_transport(monkeypatch, plan_transport)
    bind_feature_session(monkeypatch, case.origin)
    paused = invoke_cli(
        "--config", str(case.config_path), "feature", case.manifest.request, "--review"
    )
    assert paused.exit_code == 0, paused.stdout + paused.stderr
    view = parse_view(paused.stdout)
    assert view["reason"] == "review_pending"
    worktree = worktree_for(case.origin, view["feature_id"])

    # Explicit external index build in the paused managed worktree. Feature
    # execution never builds an index implicitly.
    app_context = build_app_context(case.config_path)
    index = CodeIndex(worktree, context_config=app_context.config.context)
    index.build()
    assert index.status().files and index.status().files > 0

    shadow = ContentShadow.from_tree(worktree)
    edits = intended_edit_files(case.manifest.id)
    steps = []
    for task in case.manifest.task_plan:
        for relative in task.get("files") or []:
            if relative in edits:
                steps.append(tool_completion(shadow, relative, edits[relative]))
                steps.append(final_completion(f"done-{task.get('id')}"))
    resume_transport = ScriptedTransport(steps)
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

    from benchmarks.fixtures import run_all_oracles

    assert all(item.ok for item in run_all_oracles(case.manifest, worktree))
    store = JsonCheckpointStore(worktree)
    report = project_metrics(
        feature_id=view["feature_id"],
        journal=journal_for(worktree),
        store=store,
        provenance="mock_functional",
        context_mode="v2-with-v1-fallback",
    )
    assert report.context_mode == "v2-with-v1-fallback"
    assert report.tokens.total_processed_tokens is not None
    envelope = store.load_checkpoint(view["feature_id"])
    contexts = [
        observation
        for attempt in envelope.control.closed_attempts
        for observation in attempt.context
    ]
    assert contexts
    assert any(
        (item.selected_files or 0) > 0 or (item.pack_input_tokens or 0) > 0
        for item in contexts
    )

    stats_result = run_installed(
        "stats", "feature", view["feature_id"], "--json", cwd=worktree
    )
    assert stats_result.returncode == 0, stats_result.stderr
    stats_payload = json.loads(stats_result.stdout)
    assert stats_payload["context"]["available"] is True
    assert stats_payload["usage"]["totals"]["total_tokens"] is not None
    cache = create_context_cache(app_context, workspace_root=worktree)
    return {
        "feature_id": view["feature_id"],
        "worktree": worktree,
        "sends": plan_transport.send_count + resume_transport.send_count,
        "checkout_key": cache._location().checkout_key,
    }


def test_multi_file_v2_review_index_build_then_approved_pack(
    tmp_path: Path, monkeypatch
):
    first = _run_multi_file_v2_review_index_flow(tmp_path / "first", monkeypatch)
    second = _run_multi_file_v2_review_index_flow(tmp_path / "second", monkeypatch)
    assert first["feature_id"] != second["feature_id"]
    assert first["worktree"] != second["worktree"]
    assert first["sends"] == second["sends"]
    assert first["checkout_key"] != second["checkout_key"]
