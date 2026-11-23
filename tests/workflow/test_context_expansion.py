"""Bounded current-task expansion through the existing tool loop."""

from __future__ import annotations

from pathlib import Path

import pytest

from lean_weave.app import AppContext, create_orchestrator
from lean_weave.config.models import (
    AppConfig,
    ContextConfig,
    DependencyCoverageConfig,
    ToolsConfig,
    WorkflowConfig,
)
from lean_weave.errors import ContextPreparationError
from lean_weave.executor.approval import approve_tools
from lean_weave.providers.mock import MockLLMProvider
from lean_weave.schemas import FeaturePlan, FeatureState, ImplementationTask, Stage, VerificationResult, VerificationStatus
from lean_weave.workflow.context_lifecycle import context_authority_material
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


def _adapter(
    tmp_path: Path,
    *,
    max_expansions: int = 2,
    dependency_coverage: DependencyCoverageConfig | None = None,
    files: dict[str, str] | None = None,
    write_allow: list[str] | None = None,
):
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    default_files = {
        "src/a.py": "alpha = 1\nbeta = 2\n",
        "src/dep.py": "def helper():\n    return 3\n",
    }
    for relative, text in (files or default_files).items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    allowed = write_allow or ["src/a.py"]
    config = AppConfig(
        workflow=WorkflowConfig(max_retries=1),
        tools=ToolsConfig(write_allow=allowed),
        context=ContextConfig(
            workflow_mode="v2",
            packing={
                "max_expansions": max_expansions,
                "cumulative_source_tokens": 8192,
            },
            dependency_coverage=dependency_coverage or DependencyCoverageConfig(),
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
            approved_paths=frozenset(allowed),
            approved_check_ids=frozenset(["parse"]),
        ),
        verification_runner=_runner(),
        workspace_root=root,
        journal=object(),
    )
    adapter = orchestrator._context_lifecycle  # type: ignore[attr-defined]
    adapter.bind_feature("feature")
    task_files = list(allowed)
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
                    files=task_files,
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
    return root, adapter, prepared, config, approval


def test_duplicate_range_returns_no_change_without_repeating_source(tmp_path):
    root, adapter, prepared, _config, _approval = _adapter(tmp_path)
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
    root, adapter, prepared, _config, _approval = _adapter(tmp_path, max_expansions=0)
    result = prepared.read_override(
        "read_range", {"path": "src/a.py", "start_line": 1, "end_line": 1}
    )
    assert result.status == "oversized"
    assert result.content["ok"] is False


def test_owned_patch_rebuilds_current_source_without_resetting_caps(tmp_path):
    root, adapter, prepared, _config, _approval = _adapter(tmp_path)
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
    root, adapter, prepared, _config, _approval = _adapter(tmp_path)
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
    root, adapter, prepared, _config, _approval = _adapter(tmp_path)
    (root / "src/a.py").write_text("alpha = 7\nbeta = 2\n", encoding="utf-8")
    with pytest.raises(ContextPreparationError) as exc:
        prepared.round_builder(())
    assert exc.value.category == "context_stale"
    assert exc.value.reason == "source_changed"


def test_authority_omits_dependency_coverage_when_disabled(tmp_path):
    _root, handle, _prepared, config, approval = _adapter(tmp_path)
    material = context_authority_material(config, approval, ["parse"])
    assert "dependency_coverage" not in material
    del handle


def test_authority_includes_analyzer_contract_when_enabled(tmp_path):
    _root, handle, _prepared, config, approval = _adapter(
        tmp_path,
        dependency_coverage=DependencyCoverageConfig(
            enabled=True, max_depth=1, max_definitions=4, max_tokens=256
        ),
    )
    material = context_authority_material(config, approval, ["parse"])
    coverage = material["dependency_coverage"]
    assert coverage["enabled"] is True
    assert coverage["max_depth"] == 1
    assert coverage["analyzer_contract_version"]
    del handle


def test_enabled_read_stages_forced_root_without_promoting_dependencies(tmp_path):
    files = {
        "service/__init__.py": "",
        "service/runner.py": (
            "from models.widget import HarborWidget\n\n"
            "def activate(item: HarborWidget):\n    return item.spin()\n\n"
            "def leftover():\n    return 0\n"
        ),
        "models/__init__.py": "",
        "models/widget.py": "class HarborWidget:\n    def spin(self):\n        return 1\n",
    }
    root, handle, prepared, _config, _approval = _adapter(
        tmp_path,
        files=files,
        write_allow=["service/runner.py"],
        dependency_coverage=DependencyCoverageConfig(enabled=True, max_depth=1),
    )
    assert "models/widget.py" in prepared.read_paths
    assert prepared.pack.dependency_coverage is not None
    assert any(fragment.path == "models/widget.py" for fragment in prepared.pack.fragments)
    # Read a definition that is not already in the initial pack as a forced root.
    result = prepared.read_override(
        "read_symbol",
        {"path": "service/runner.py", "name": "leftover"},
    )
    assert result.status == "ok"
    assert result.content.get("status") == "ok"
    assert result.content.get("staged_for_round") is True
    assert result.content.get("forced_root_only") is True
    assert len(handle._forced) == 1
    assert handle._forced[0].path == "service/runner.py"
    # Dependency fragments from the pack must not become depth-zero forced roots.
    assert all(ref.path == "service/runner.py" for ref in handle._forced)
    request = prepared.round_builder(())
    content = request["messages"][-1]["content"]
    assert "class HarborWidget" in content
    assert "def leftover" in content
    del root


def test_dependency_limit_refusal_maps_to_budget_exhausted(tmp_path):
    from lean_weave.context.packing_models import PackResult

    _root, adapter, _prepared, _config, _approval = _adapter(
        tmp_path,
        dependency_coverage=DependencyCoverageConfig(enabled=True, max_depth=1),
    )
    error = adapter._refusal(
        PackResult(status="insufficient_context", reason_codes=("dependency_limit:tokens",))
    )
    assert error.category == "budget_exhausted"
    assert error.reason == "dependency_limit:tokens"


def test_dependency_limit_on_read_maps_to_oversized(tmp_path, monkeypatch):
    from lean_weave.context.packing_models import PackResult

    _root, adapter, prepared, _config, _approval = _adapter(
        tmp_path,
        dependency_coverage=DependencyCoverageConfig(enabled=True, max_depth=1),
    )

    def fake_expand(session, request, **kwargs):
        del session, request, kwargs
        return PackResult(
            status="insufficient_context",
            reason_codes=("dependency_limit:definitions",),
        )

    monkeypatch.setattr(adapter.packer, "expand", fake_expand)
    result = prepared.read_override(
        "read_symbol",
        {"path": "src/a.py", "name": "alpha"},
    )
    assert result.status == "oversized"
    assert result.content["ok"] is False
