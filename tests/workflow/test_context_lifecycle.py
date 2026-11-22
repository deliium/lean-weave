"""Trusted V2 stage projection through the existing orchestrator."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from lean_weave.app import AppContext, create_orchestrator
from lean_weave.config.models import AppConfig, ContextConfig, ToolsConfig, WorkflowConfig
from lean_weave.errors import ContextPreparationError
from lean_weave.executor.approval import approve_tools
from lean_weave.providers.mock import MockLLMProvider
from lean_weave.schemas import (
    SCHEMA_VERSION,
    FeaturePlan,
    FeatureState,
    ImplementationTask,
    Stage,
    TokenUsage,
    VerificationResult,
    VerificationStatus,
)
from lean_weave.workflow.context_metrics import RecordingContextObserver
from lean_weave.workflow.planning import PlanPolicy
from lean_weave.workflow.state import JsonCheckpointStore


class ScriptedRunner:
    def __init__(self, results):
        self.results = list(results)

    def preflight(self):
        return None

    def run(self, feature_state):
        return self.results.pop(0)


def usage() -> TokenUsage:
    return TokenUsage(prompt_tokens=4, completion_tokens=6, total_tokens=10)


def final(content: str = "done") -> dict:
    return {
        "message": {"role": "assistant", "content": content},
        "usage": usage(),
        "usage_reported": True,
    }


def plan_response(files=("src/a.py",), description="edit a") -> dict:
    return {
        "message": {"role": "assistant", "content": None},
        "structured": {
            "version": SCHEMA_VERSION,
            "goal": "edit",
            "settings": {"testing": True, "logging": "verbose", "docs": True},
            "reconciliation": [],
            "tasks": [
                {
                    "id": "1",
                    "title": "edit",
                    "description": description,
                    "files": list(files),
                    "depends_on": [],
                }
            ],
            "commit_checkpoints": [],
        },
        "usage": usage(),
        "usage_reported": True,
    }


def raw_revision(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def make_case(tmp_path: Path, *, files: dict[str, str | bytes], mode: str = "v2"):
    root = tmp_path / "ws"
    root.mkdir()
    for path, value in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, bytes):
            target.write_bytes(value)
        else:
            target.write_text(value, encoding="utf-8")
    config = AppConfig(
        workflow=WorkflowConfig(max_retries=1, max_task_tokens=20000),
        tools=ToolsConfig(write_allow=["src/a.py"]),
        context=ContextConfig(workflow_mode=mode),
    )
    approval = approve_tools(root, config, allow_writes=True, allow_checks=True)
    store = JsonCheckpointStore(root)
    policy = PlanPolicy(
        approved_paths=frozenset(["src/a.py"]), approved_check_ids=frozenset(["parse"])
    )
    return root, config, approval, store, policy


def make_adapter(tmp_path, *, extra_files=None, mode="v2", observer=None):
    files = {"src/a.py": "value = 1\n"} if extra_files is None else extra_files
    root, config, approval, store, policy = make_case(tmp_path, files=files, mode=mode)
    context = AppContext(
        config=config,
        config_source=None,
        llm_provider=MockLLMProvider([]),
        state_store=store,
    )
    orchestrator = create_orchestrator(
        context,
        approval=approval,
        check_ids=["parse"],
        plan_policy=policy,
        verification_runner=ScriptedRunner([VerificationResult(status=VerificationStatus.PASS)]),
        workspace_root=root,
        journal=object(),
        context_observer=observer,
    )
    return root, config, approval, store, policy, orchestrator


def test_v2_plan_is_map_only_and_implement_is_active_task_scoped(tmp_path):
    root, config, approval, store, policy, orchestrator = make_adapter(
        tmp_path,
        extra_files={
            "src/a.py": "value = 1\n",
            "src/other.py": "unrelated = 99\n",
        },
    )
    provider = MockLLMProvider([plan_response(), final()])
    orchestrator._provider = provider  # type: ignore[attr-defined]
    state = orchestrator.run_feature("feature", "edit a")
    assert state.stage.value == "done"
    assert len(provider.requests) == 2

    plan_text = str(provider.requests[0]["messages"])
    assert "edit a" in plan_text
    assert "src/a.py" in plan_text
    assert "a :: src/a.py" in plan_text
    assert "value = 1" not in plan_text
    assert "unrelated" not in plan_text

    implement_text = str(provider.requests[1]["messages"])
    assert "title: edit" in implement_text
    assert "value = 1" in implement_text
    assert "unrelated = 99" not in implement_text
    assert "task_specification" in implement_text


def test_editable_revision_is_raw_bytes_and_only_for_task_files(tmp_path):
    crlf = b"value = 1\r\n"
    root, config, approval, store, policy, orchestrator = make_adapter(
        tmp_path,
        extra_files={
            "src/a.py": crlf,
            "src/dep.py": "DEP = 1\n",
        },
    )
    provider = MockLLMProvider([plan_response(), final()])
    orchestrator._provider = provider  # type: ignore[attr-defined]
    state = orchestrator.run_feature("revision", "edit a")
    assert state.stage.value == "done"
    content = provider.requests[1]["messages"][-1]["content"]
    assert f"src/a.py {raw_revision(crlf)}" in content
    assert "src/dep.py" not in content.split("<editable_revisions>", 1)[1]


def test_v2_empty_map_is_valid_for_planning(tmp_path):
    root, config, approval, store, policy = make_case(
        tmp_path, files={"src/a.py": "value = 1\n"}
    )
    # No approved source paths: a metadata-only plan is still valid.
    empty_policy = PlanPolicy(
        approved_paths=frozenset(), approved_check_ids=frozenset(["parse"])
    )
    provider = MockLLMProvider([plan_response(files=[]), final()])
    context = AppContext(
        config=config,
        config_source=None,
        llm_provider=MockLLMProvider([]),
        state_store=store,
    )
    orchestrator = create_orchestrator(
        context,
        approval=approval,
        check_ids=["parse"],
        plan_policy=empty_policy,
        verification_runner=ScriptedRunner(
            [VerificationResult(status=VerificationStatus.PASS)]
        ),
        workspace_root=root,
        journal=object(),
    )
    orchestrator._provider = provider  # type: ignore[attr-defined]
    state = orchestrator.run_feature("empty-map", "edit a")
    assert "repository_map" not in str(provider.requests[0]["messages"])
    assert state.stage.value == "failed"
    assert orchestrator.result("empty-map").reason == "context_insufficient"


def test_required_source_denied_is_a_typed_local_refusal(tmp_path):
    root, config, approval, store, policy, orchestrator = make_adapter(
        tmp_path, extra_files={"src/a.py": "value = 1\n"}
    )
    config.context.retrieval.top_k = 1
    provider = MockLLMProvider([plan_response(), final()])
    orchestrator._provider = provider  # type: ignore[attr-defined]
    adapter = orchestrator._context_lifecycle  # type: ignore[attr-defined]
    state = FeatureState(
        feature_id="denied",
        request="edit a",
        plan=FeaturePlan(
            goal="g",
            tasks=[ImplementationTask(id="1", title="edit", files=["src/missing.py"])],
        ),
        active_task_id="1",
        stage=Stage.IMPLEMENT,
    )
    with pytest.raises(ContextPreparationError) as exc:
        adapter.prepare_tool_stage(
            stage="implement",
            state=state,
            task=state.plan.tasks[0],
            schemas=(),
            fragments=(),
        )
    assert exc.value.category == "context_insufficient"


def test_observer_records_safe_bounded_stage_events(tmp_path):
    observer = RecordingContextObserver()
    root, config, approval, store, policy, orchestrator = make_adapter(
        tmp_path, observer=observer
    )
    provider = MockLLMProvider([plan_response(), final()])
    orchestrator._provider = provider  # type: ignore[attr-defined]
    state = orchestrator.run_feature("observed", "edit a")
    assert state.stage.value == "done"
    assert [event.stage for event in observer.events] == ["plan", "implement"]
    for event in observer.events:
        assert event.mode == "v2"
        assert event.outcome in {"planned", "done"}
        assert event.wire_input_estimate > 0
        assert all(isinstance(item.ordinal, int) for item in event.fragments)
        assert "src/a.py" not in repr(event)


def test_v1_default_does_not_construct_workflow_adapter(tmp_path):
    root, config, approval, store, policy = make_case(
        tmp_path, files={"src/a.py": "value = 1\n"}, mode="v1"
    )
    context = AppContext(
        config=config,
        config_source=None,
        llm_provider=MockLLMProvider([]),
        state_store=store,
    )
    orchestrator = create_orchestrator(
        context,
        approval=approval,
        check_ids=["parse"],
        plan_policy=policy,
        verification_runner=ScriptedRunner(
            [VerificationResult(status=VerificationStatus.PASS)]
        ),
        workspace_root=root,
        journal=object(),
    )
    assert orchestrator._context_lifecycle is None  # type: ignore[attr-defined]


def test_v2_fallback_uses_v1_builder_for_no_complete_anchor(tmp_path):
    root, config, approval, store, policy, orchestrator = make_adapter(
        tmp_path,
        extra_files={"src/a.py": ""},
        mode="v2-with-v1-fallback",
    )
    provider = MockLLMProvider([plan_response(), final()])
    orchestrator._provider = provider  # type: ignore[attr-defined]
    state = orchestrator.run_feature("fallback", "edit a")
    assert state.stage.value == "done"
    assert len(provider.requests) == 2
    assert "system-implement" not in str(provider.requests[1]["messages"])


def test_v2_planning_bypass_runs_existing_simple_path(tmp_path):
    from lean_weave.workflow.planning import SingleTaskSpec

    root, config, approval, store, policy, orchestrator = make_adapter(tmp_path)
    provider = MockLLMProvider([final()])
    orchestrator._provider = provider  # type: ignore[attr-defined]
    spec = SingleTaskSpec(
        id="1", title="edit", file="src/a.py", check_ids=["parse"]
    )
    state = orchestrator.run_simple("bypass", "edit a", spec)
    assert state.stage.value == "done"
    assert len(provider.requests) == 1
    assert provider.requests[0]["stage"] == "implement"
    assert "value = 1" in provider.requests[0]["messages"][-1]["content"]


def test_v2_strict_refuses_no_complete_anchor_before_implement_dispatch(tmp_path):
    root, config, approval, store, policy, orchestrator = make_adapter(
        tmp_path,
        extra_files={"src/a.py": ""},
        mode="v2",
    )
    provider = MockLLMProvider([plan_response()])
    orchestrator._provider = provider  # type: ignore[attr-defined]
    state = orchestrator.run_feature("strict", "edit a")
    assert state.stage.value != "done"
    assert state.stage.value in {"failed", "interrupted"}
    assert len(provider.requests) == 1
    assert "system-implement" not in str(provider.requests[0]["messages"])
