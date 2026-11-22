"""Bounded current-task expansion through the existing tool loop."""

from __future__ import annotations

from pathlib import Path

from lean_weave.app import AppContext, create_orchestrator
from lean_weave.config.models import AppConfig, ContextConfig, ToolsConfig, WorkflowConfig
from lean_weave.executor.approval import approve_tools
from lean_weave.providers.mock import MockLLMProvider
from lean_weave.schemas import FeaturePlan, FeatureState, ImplementationTask, Stage, VerificationResult, VerificationStatus
from lean_weave.workflow.planning import PlanPolicy
from lean_weave.workflow.state import JsonCheckpointStore
from lean_weave.workflow.tools import TOOL_SCHEMAS


def _runner():
    class Runner:
        def preflight(self):
            return None

        def run(self, feature_state):
            return VerificationResult(status=VerificationStatus.PASS)

    return Runner()


def _adapter(tmp_path: Path, *, max_expansions: int = 2):
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "src/a.py").write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")
    (root / "src/dep.py").write_text("def helper():\n    return 3\n", encoding="utf-8")
    config = AppConfig(
        workflow=WorkflowConfig(max_retries=1),
        tools=ToolsConfig(write_allow=["src/a.py"]),
        context=ContextConfig(
            workflow_mode="v2",
            packing={
                "max_expansions": max_expansions,
                "cumulative_source_tokens": 8192,
            },
        ),
    )
    approval = approve_tools(root, config, allow_writes=True, allow_checks=True)
    context = AppContext(
        config=config,
        config_source=None,
        llm_provider=MockLLMProvider([]),
        state_store=JsonCheckpointStore(root),
    )
    orchestrator = create_orchestrator(
        context,
        approval=approval,
        check_ids=["parse"],
        plan_policy=PlanPolicy(
            approved_paths=frozenset(["src/a.py"]),
            approved_check_ids=frozenset(["parse"]),
        ),
        verification_runner=_runner(),
        workspace_root=root,
        journal=object(),
    )
    adapter = orchestrator._context_lifecycle  # type: ignore[attr-defined]
    adapter.bind_feature("feature")
    state = FeatureState(
        feature_id="feature",
        stage=Stage.IMPLEMENT,
        request="edit a",
        active_task_id="1",
        plan=FeaturePlan(
            goal="g",
            settings={"testing": True},
            tasks=[
                ImplementationTask(
                    id="1",
                    title="edit",
                    description="edit a",
                    files=["src/a.py"],
                )
            ],
        ),
    )
    prepared = adapter.prepare_tool_stage(
        stage="implement",
        state=state,
        task=state.plan.tasks[0],
        schemas=TOOL_SCHEMAS,
        fragments=(),
    )
    return root, adapter, prepared


def test_duplicate_range_returns_no_change_without_repeating_source(tmp_path):
    root, adapter, prepared = _adapter(tmp_path)
    first = prepared.read_override(
        "read_range", {"path": "src/a.py", "start_line": 1, "end_line": 2}
    )
    assert first.status == "ok"
    session = adapter._session
    assert session.attempts == 1
    second = prepared.read_override(
        "read_range", {"path": "src/a.py", "start_line": 1, "end_line": 2}
    )
    assert second.status == "ok"
    assert second.content["status"] == "no_change"
    assert session.attempts == 2
    request = prepared.round_builder(())
    content = request["messages"][-1]["content"]
    assert content.count("alpha = 1") == 1


def test_expansion_attempt_limit_is_typed_and_does_not_dispatch(tmp_path):
    root, adapter, prepared = _adapter(tmp_path, max_expansions=0)
    result = prepared.read_override(
        "read_range", {"path": "src/a.py", "start_line": 1, "end_line": 1}
    )
    assert result.status == "oversized"
    assert result.content["ok"] is False


def test_owned_patch_rebuilds_current_source_without_resetting_caps(tmp_path):
    root, adapter, prepared = _adapter(tmp_path)
    first = prepared.read_override(
        "read_range", {"path": "src/a.py", "start_line": 2, "end_line": 2}
    )
    assert first.status == "ok"
    attempts = adapter._session.attempts
    (root / "src/a.py").write_text("alpha = 9\nbeta = 2\n", encoding="utf-8")
    adapter.note_patch(["src/a.py"])
    request = prepared.round_builder(())
    content = request["messages"][-1]["content"]
    assert "alpha = 9" in content
    assert "alpha = 1" not in content
    assert adapter._session.attempts == attempts


def test_retry_expansion_is_limited_to_attributed_paths(tmp_path):
    root, adapter, prepared = _adapter(tmp_path)
    state = FeatureState(
        feature_id="retry",
        stage=Stage.RETRY,
        request="fix",
        active_task_id="1",
        plan=FeaturePlan(
            goal="g",
            tasks=[
                ImplementationTask(
                    id="1", title="edit", description="fix", files=["src/a.py"]
                )
            ],
        ),
        verification=VerificationResult(
            status=VerificationStatus.FAIL,
            blocking=True,
            blockers=[{"code": "E999", "path": "src/a.py", "line": 1}],
            affected_files=["src/a.py"],
        ),
    )
    retry = adapter.prepare_tool_stage(
        stage="retry",
        state=state,
        task=state.plan.tasks[0],
        schemas=TOOL_SCHEMAS,
        fragments=(),
    )
    assert retry.read_paths == ("src/a.py",)
    denied = retry.read_override(
        "read_range", {"path": "src/dep.py", "start_line": 1, "end_line": 1}
    )
    assert denied.status == "denied"


def test_external_change_before_first_send_is_stale(tmp_path):
    import pytest
    from lean_weave.errors import ContextPreparationError

    root, adapter, prepared = _adapter(tmp_path)
    (root / "src/a.py").write_text("alpha = 7\nbeta = 2\n", encoding="utf-8")
    with pytest.raises(ContextPreparationError) as exc:
        prepared.round_builder(())
    assert exc.value.category == "context_stale"
    assert exc.value.reason == "source_changed"
