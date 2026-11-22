"""Feature-worktree cache checks with the existing offline gateway driver."""

from __future__ import annotations

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
