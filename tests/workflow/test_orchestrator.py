import pytest

from lean_weave.config.models import AppConfig, ToolsConfig, WorkflowConfig
from lean_weave.errors import (
    BudgetExceededError,
    InvalidTransition,
    PlanValidationError,
    ProviderError,
)
from lean_weave.executor.approval import approve_tools
from lean_weave.providers.mock import MockLLMProvider
from lean_weave.schemas import (
    SCHEMA_VERSION,
    FeaturePlan,
    FeatureState,
    ImplementationTask,
    Stage,
    TaskStatus,
    TokenUsage,
    VerificationResult,
    VerificationStatus,
)
from lean_weave.schemas.models import TRANSITIONS
from lean_weave.verify.runner import ApprovedChecksRunner
from lean_weave.workflow.orchestrator import MinimalWorkflowOrchestrator
from lean_weave.workflow.planning import PlanPolicy, SingleTaskSpec
from lean_weave.workflow.results import WorkflowStatus
from lean_weave.workflow.stages import pending_dependency_failure, ready_task, transition
from lean_weave.workflow.state import (
    ControlRecord,
    EffectKind,
    JsonCheckpointStore,
    PendingEffect,
)
from lean_weave.workflow.tools import WorkflowTools


class FakeContext:
    def __init__(self):
        self.stages = []

    def build(self, stage, feature_state):
        self.stages.append(stage)
        return {
            "stage": stage,
            "messages": [
                {"role": "system", "content": f"system-{stage}"},
                {"role": "user", "content": f"user-{stage}"},
            ],
            "system_prompt": f"system-{stage}",
            "user_prompt": f"user-{stage}",
            "estimated_tokens": 10,
        }


class ScriptedRunner:
    def __init__(self, results):
        self.results = list(results)
        self.preflight_calls = 0
        self.run_calls = 0

    def preflight(self):
        self.preflight_calls += 1
        return None

    def run(self, feature_state):
        self.run_calls += 1
        return self.results.pop(0)


def _approval(tmp_path):
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "src/a.py").write_text("value = 1\n", encoding="utf-8")
    config = AppConfig(tools=ToolsConfig(write_allow=["src/a.py"]))
    return approve_tools(root, config, allow_writes=True, allow_checks=True)


def _policy(paths=("src/a.py",), checks=("parse",)):
    return PlanPolicy(
        approved_paths=frozenset(paths),
        approved_check_ids=frozenset(checks),
    )


def _usage(total=10):
    return TokenUsage(prompt_tokens=4, completion_tokens=6, total_tokens=total)


def _final(content="done"):
    return {
        "message": {"role": "assistant", "content": content},
        "usage": _usage(),
        "usage_reported": True,
    }


def _plan_response(tasks=None):
    return {
        "message": {"role": "assistant", "content": None},
        "structured": {
            "version": SCHEMA_VERSION,
            "goal": "edit one file",
            "settings": {"testing": True, "logging": "verbose", "docs": True},
            "reconciliation": [],
            "tasks": tasks
            or [
                {
                    "id": "1",
                    "title": "edit",
                    "description": "change a",
                    "files": ["src/a.py"],
                    "depends_on": [],
                }
            ],
            "commit_checkpoints": [],
        },
        "usage": _usage(),
        "usage_reported": True,
    }


def _orchestrator(tmp_path, provider, runner, *, config=None, context=None):
    store = JsonCheckpointStore(tmp_path)
    approval = _approval(tmp_path)
    tools = WorkflowTools(approval, task_files=[], checks_ready=True)
    return MinimalWorkflowOrchestrator(
        config=config or WorkflowConfig(max_retries=2),
        llm_provider=provider,
        context_provider=context or FakeContext(),
        state_store=store,
        verification_runner=runner,
        tools=tools,
        plan_policy=_policy(),
    ), store, approval


def _seed(store, approval, state, control=None):
    store.create_checkpoint(
        state,
        control or ControlRecord(feature_id=state.feature_id, workspace=store.identity),
    )


def test_transition_table_is_enforced():
    for (previous, event), target in TRANSITIONS.items():
        state = FeatureState(feature_id="f", stage=previous)
        assert transition(state, event) is target
        assert state.stage is target
    with pytest.raises(InvalidTransition):
        transition(FeatureState(feature_id="f", stage=Stage.PLAN), "pass")
    with pytest.raises(InvalidTransition):
        transition(FeatureState(feature_id="f", stage=Stage.DONE), "planned")


def test_ready_task_and_failed_dependencies():
    plan = FeaturePlan(
        goal="g",
        tasks=[
            ImplementationTask(id="a", title="a", depends_on=[]),
            ImplementationTask(
                id="b", title="b", depends_on=["a"], status=TaskStatus.FAILED
            ),
            ImplementationTask(id="c", title="c", depends_on=["b"]),
        ],
    )
    assert ready_task(plan, None).id == "a"
    assert pending_dependency_failure(plan) is True
    plan.tasks[0].status = TaskStatus.DONE
    assert ready_task(plan, None) is None
    assert pending_dependency_failure(plan) is True


def test_detailed_run_success_uses_stage_scoped_messages(tmp_path):
    provider = MockLLMProvider([_plan_response(), _final()])
    runner = ScriptedRunner([VerificationResult(status=VerificationStatus.PASS)])
    orchestrator, store, approval = _orchestrator(tmp_path, provider, runner)
    state = orchestrator.run_feature("f", "edit a")
    assert state.stage is Stage.DONE
    assert state.verification.status is VerificationStatus.PASS
    assert len(provider.requests) == 2
    assert provider.requests[0]["stage"] == "plan"
    assert provider.requests[1]["stage"] == "implement"
    assert "user-plan" not in str(provider.requests[1]["messages"])
    assert "user-implement" in str(provider.requests[1]["messages"])
    result = orchestrator.result("f")
    assert result.status is WorkflowStatus.SUCCESS
    assert result.reason == "verification_passed"
    assert result.tasks[0].status is TaskStatus.DONE
    with pytest.raises(Exception):
        store.load_checkpoint("missing")


def test_simple_path_bypasses_planning_and_invalid_input_fails_before_state(tmp_path):
    provider = MockLLMProvider([_final()])
    runner = ScriptedRunner([VerificationResult(status=VerificationStatus.PASS)])
    orchestrator, store, approval = _orchestrator(tmp_path, provider, runner)
    spec = SingleTaskSpec(id="1", title="edit", file="src/a.py", check_ids=["parse"])
    state = orchestrator.run_simple("simple", "edit a", spec)
    assert state.stage is Stage.DONE
    assert len(provider.requests) == 1
    assert provider.requests[0]["stage"] == "implement"
    assert store.load_checkpoint("simple").control.plan_spend.calls == 0

    bad = spec.model_copy(update={"file": "outside.py"})
    with pytest.raises(PlanValidationError):
        orchestrator.run_simple("bad", "edit a", bad)
    assert not store.exists("bad")
    assert len(provider.requests) == 1


def test_retry_repairs_only_the_attributed_task(tmp_path):
    provider = MockLLMProvider([_final(), _final("fixed")])
    runner = ScriptedRunner(
        [
            VerificationResult(
                status=VerificationStatus.FAIL,
                blocking=True,
                blockers=[{"code": "E999", "path": "src/a.py", "line": 1}],
                affected_files=["src/a.py"],
            ),
            VerificationResult(status=VerificationStatus.PASS),
        ]
    )
    orchestrator, store, approval = _orchestrator(tmp_path, provider, runner)
    spec = SingleTaskSpec(id="1", title="edit", file="src/a.py", check_ids=["parse"])
    state = orchestrator.run_simple("retry", "edit a", spec)
    assert state.stage is Stage.DONE
    assert state.retry_count == 1
    assert [request["stage"] for request in provider.requests] == ["implement", "retry"]
    envelope = store.load_checkpoint("retry")
    assert envelope.control.terminal_status is WorkflowStatus.SUCCESS


def test_retry_cap_and_ambiguous_diagnostics_fail_closed(tmp_path):
    provider = MockLLMProvider([_final(), _final("fixed")])
    failure = VerificationResult(
        status=VerificationStatus.FAIL,
        blocking=True,
        blockers=[{"code": "E999", "path": "src/a.py", "line": 1}],
        affected_files=["src/a.py"],
    )
    runner = ScriptedRunner([failure, failure])
    config = WorkflowConfig(max_retries=1)
    orchestrator, store, _approval_obj = _orchestrator(
        tmp_path, provider, runner, config=config
    )
    spec = SingleTaskSpec(id="1", title="edit", file="src/a.py", check_ids=["parse"])
    state = orchestrator.run_simple("cap", "edit a", spec)
    assert state.stage is Stage.FAILED
    result = orchestrator.result("cap")
    assert result.status is WorkflowStatus.FAILED
    assert result.reason == "retry_exhausted"

    ambiguous_provider = MockLLMProvider([_final()])
    ambiguous = VerificationResult(
        status=VerificationStatus.FAIL,
        blocking=True,
        blockers=[{"code": "E999"}],
        affected_files=[],
    )
    orchestrator2, _store2, _approval2 = _orchestrator(
        tmp_path / "ambiguous", ambiguous_provider, ScriptedRunner([ambiguous])
    )
    spec2 = SingleTaskSpec(id="1", title="edit", file="src/a.py", check_ids=["parse"])
    state2 = orchestrator2.run_simple("ambiguous", "edit a", spec2)
    assert state2.stage is Stage.FAILED
    assert orchestrator2.result("ambiguous").reason == "verification_failed"


def test_budget_exhaustion_is_a_separate_status(tmp_path):
    provider = MockLLMProvider([_final()])
    runner = ScriptedRunner([VerificationResult(status=VerificationStatus.PASS)])
    config = WorkflowConfig(max_task_tokens=1, feature_token_budget=100)
    orchestrator, _store, _approval_obj = _orchestrator(
        tmp_path, provider, runner, config=config
    )
    spec = SingleTaskSpec(id="1", title="edit", file="src/a.py", check_ids=["parse"])
    state = orchestrator.run_simple("budget", "edit a", spec)
    assert state.stage is Stage.FAILED
    result = orchestrator.result("budget")
    assert result.status is WorkflowStatus.BUDGET_EXHAUSTED
    assert provider.requests == []


def test_gateway_budget_denial_terminates_budget_exhausted(tmp_path):
    provider = MockLLMProvider([BudgetExceededError("gateway denied the call")])
    runner = ScriptedRunner([])
    orchestrator, store, _approval_obj = _orchestrator(tmp_path, provider, runner)
    spec = SingleTaskSpec(id="1", title="edit", file="src/a.py", check_ids=["parse"])

    state = orchestrator.run_simple("gateway-budget", "edit a", spec)

    assert state.stage is Stage.FAILED
    assert len(provider.requests) == 1
    result = orchestrator.result("gateway-budget")
    assert result.status is WorkflowStatus.BUDGET_EXHAUSTED
    assert result.reason == "gateway_budget_denied"
    control = store.load_checkpoint("gateway-budget").control
    assert control.terminal_status is WorkflowStatus.BUDGET_EXHAUSTED
    assert control.pending_effect is None


def test_plan_stage_reported_overage_terminates_budget_exhausted(tmp_path):
    plan = _plan_response()
    plan["usage"] = TokenUsage(
        prompt_tokens=2500, completion_tokens=2500, total_tokens=5000
    )
    provider = MockLLMProvider([plan])
    runner = ScriptedRunner([])
    orchestrator, _store, _approval_obj = _orchestrator(
        tmp_path,
        provider,
        runner,
        config=WorkflowConfig(feature_token_budget=2500, max_plan_tokens=10000),
    )

    state = orchestrator.run_feature("plan-budget", "edit a")

    assert state.stage is Stage.FAILED
    assert len(provider.requests) == 1
    result = orchestrator.result("plan-budget")
    assert result.status is WorkflowStatus.BUDGET_EXHAUSTED
    assert result.reason == "feature_token_overage"


def test_warn_verification_without_blocking_reaches_done(tmp_path):
    provider = MockLLMProvider([_final()])
    runner = ScriptedRunner(
        [VerificationResult(status=VerificationStatus.WARN, blocking=False)]
    )
    orchestrator, _store, _approval_obj = _orchestrator(tmp_path, provider, runner)
    spec = SingleTaskSpec(id="1", title="edit", file="src/a.py", check_ids=["parse"])

    state = orchestrator.run_simple("warn", "edit a", spec)

    assert state.stage is Stage.DONE
    assert orchestrator.result("warn").status is WorkflowStatus.SUCCESS
    assert len(provider.requests) == 1


def test_terminal_resume_returns_without_dispatch(tmp_path):
    provider = MockLLMProvider([])
    runner = ScriptedRunner([])
    orchestrator, store, approval = _orchestrator(tmp_path, provider, runner)
    state = FeatureState(feature_id="done", stage=Stage.DONE)
    control = ControlRecord(feature_id="done", workspace=store.identity)
    control.terminal_status = WorkflowStatus.SUCCESS
    control.terminal_reason = "verification_passed"
    _seed(store, approval, state, control)
    resumed = orchestrator.resume("done")
    assert resumed.stage is Stage.DONE
    assert provider.requests == []
    assert runner.run_calls == 0


def test_pending_effect_interrupts_and_never_replays(tmp_path):
    provider = MockLLMProvider([])
    runner = ScriptedRunner([])
    orchestrator, store, approval = _orchestrator(tmp_path, provider, runner)
    state = FeatureState(feature_id="effect", stage=Stage.IMPLEMENT)
    control = ControlRecord(feature_id="effect", workspace=store.identity)
    control.pending_effect = PendingEffect(
        kind=EffectKind.PATCH,
        operation_id="patch-1",
        paths=["src/a.py"],
        confirmed=False,
    )
    _seed(store, approval, state, control)
    resumed = orchestrator.resume("effect")
    assert resumed.stage is Stage.INTERRUPTED
    assert resumed.interrupt["reason"] == "unresolved_effect"
    assert store.load_checkpoint("effect").control.pending_effect is not None
    assert provider.requests == []
    assert runner.run_calls == 0


def test_provider_error_interrupts_and_resume_continues(tmp_path):
    provider = MockLLMProvider([ProviderError("transport failed"), _final()])
    runner = ScriptedRunner([VerificationResult(status=VerificationStatus.PASS)])
    orchestrator, store, approval = _orchestrator(tmp_path, provider, runner)
    state = FeatureState(
        feature_id="resume",
        stage=Stage.IMPLEMENT,
        plan=FeaturePlan(
            goal="g",
            tasks=[
                ImplementationTask(
                    id="1", title="edit", files=["src/a.py"]
                )
            ],
        ),
        active_task_id="1",
    )
    _seed(store, approval, state)
    first = orchestrator.resume("resume")
    assert first.stage is Stage.INTERRUPTED
    assert first.interrupt["reason"] == "provider_error"
    second = orchestrator.resume("resume")
    assert second.stage is Stage.DONE
    assert [request["stage"] for request in provider.requests] == ["implement", "implement"]
    assert orchestrator.result("resume").status is WorkflowStatus.SUCCESS


def test_failed_dependency_is_terminal(tmp_path):
    provider = MockLLMProvider([])
    runner = ScriptedRunner([])
    orchestrator, store, approval = _orchestrator(tmp_path, provider, runner)
    state = FeatureState(
        feature_id="dep",
        stage=Stage.IMPLEMENT,
        plan=FeaturePlan(
            goal="g",
            tasks=[
                ImplementationTask(id="a", title="a", status=TaskStatus.FAILED),
                ImplementationTask(id="b", title="b", depends_on=["a"]),
            ],
        ),
    )
    _seed(store, approval, state)
    result = orchestrator.resume("dep")
    assert result.stage is Stage.FAILED
    assert orchestrator.result("dep").reason == "failed_dependency"
    assert provider.requests == []


def test_resume_revalidates_approval(tmp_path):
    provider = MockLLMProvider([])
    runner = ScriptedRunner([])
    store = JsonCheckpointStore(tmp_path)
    state = FeatureState(feature_id="approval", stage=Stage.IMPLEMENT)
    _seed(store, None, state)
    orchestrator = MinimalWorkflowOrchestrator(
        config=WorkflowConfig(),
        llm_provider=provider,
        context_provider=FakeContext(),
        state_store=store,
        verification_runner=runner,
        tools=None,
        plan_policy=_policy(),
    )
    result = orchestrator.resume("approval")
    assert result.stage is Stage.INTERRUPTED
    assert result.interrupt["reason"] == "approval_required"
    assert provider.requests == []
