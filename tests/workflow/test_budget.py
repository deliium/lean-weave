import pytest

from lean_weave.errors import WorkflowBudgetExceeded
from lean_weave.providers.mock import MockLLMProvider
from lean_weave.schemas import FeatureState, TokenUsage
from lean_weave.workflow.budget import (
    FEATURE_TOKEN_LIMIT,
    FEATURE_TOKEN_OVERAGE,
    TASK_CALL_LIMIT,
    BudgetLimits,
    WorkflowBudget,
    settle_charge,
)
from lean_weave.workflow.state import ControlRecord, JsonCheckpointStore
from lean_weave.workflow.tool_loop import run_tool_loop
from lean_weave.workflow.tools import ToolCallResult


def _limits(**kwargs):
    values = {
        "max_plan_calls": 1,
        "max_plan_tokens": 10_000,
        "max_task_calls": 4,
        "max_task_tokens": 10_000,
        "feature_tokens": 20_000,
    }
    values.update(kwargs)
    return BudgetLimits(**values)


def _control(store=None, feature_id="f"):
    workspace = store.identity if store is not None else "sha256:" + "0" * 64
    return ControlRecord(feature_id=feature_id, workspace=workspace)


def _response(total=10):
    return {
        "message": {"role": "assistant", "content": "done"},
        "usage": TokenUsage(prompt_tokens=4, completion_tokens=6, total_tokens=total),
        "usage_reported": True,
    }


def _reserve(budget, scope="1", messages=None):
    return budget.reserve(
        scope,
        messages=messages or [{"role": "user", "content": "work"}],
        output_cap=50,
    )


def test_missing_usage_keeps_the_reservation():
    control = _control()
    budget = WorkflowBudget("f", control, limits=_limits())
    reservation = _reserve(budget)
    budget.settle(reservation, None, reported=False)
    assert control.task_spend["1"].calls == 1
    assert control.task_spend["1"].tokens == reservation.charge
    assert settle_charge(None, reserved=7, reported=False).charge == 7


def test_measured_usage_replaces_the_reservation():
    control = _control()
    budget = WorkflowBudget("f", control, limits=_limits())
    reservation = _reserve(budget)
    budget.settle(reservation, TokenUsage(prompt_tokens=4, completion_tokens=6, total_tokens=10))
    assert control.task_spend["1"].tokens == 10
    incomplete = settle_charge({"prompt_tokens": 5}, reserved=100)
    assert incomplete.charge == 100
    assert incomplete.measured is False


def test_failed_attempt_keeps_the_debit_and_blocks_a_second_dispatch():
    control = _control()
    budget = WorkflowBudget("f", control, limits=_limits(max_task_calls=1))
    provider = MockLLMProvider([ValueError("transport failed")])
    with pytest.raises(ValueError):
        run_tool_loop(
            provider,
            budget,
            feature_id="f",
            stage="implement",
            scope="1",
            messages=[{"role": "user", "content": "work"}],
            tool_schemas=None,
            dispatch=None,
            max_rounds=2,
            max_calls_per_batch=2,
            output_cap=50,
        )
    assert control.task_spend["1"].calls == 1
    assert len(provider.requests) == 1
    with pytest.raises(WorkflowBudgetExceeded, match=TASK_CALL_LIMIT):
        run_tool_loop(
            provider,
            budget,
            feature_id="f",
            stage="implement",
            scope="1",
            messages=[{"role": "user", "content": "work"}],
            tool_schemas=None,
            dispatch=None,
            max_rounds=2,
            max_calls_per_batch=2,
            output_cap=50,
        )
    assert len(provider.requests) == 1


def test_refusal_happens_before_dispatch_with_mock_provider():
    control = _control()
    budget = WorkflowBudget("f", control, limits=_limits(max_task_tokens=1))
    provider = MockLLMProvider([_response()])
    with pytest.raises(WorkflowBudgetExceeded, match="task_token_limit"):
        run_tool_loop(
            provider,
            budget,
            feature_id="f",
            stage="implement",
            scope="1",
            messages=[{"role": "user", "content": "work"}],
            tool_schemas=None,
            dispatch=None,
            max_rounds=2,
            max_calls_per_batch=2,
            output_cap=50,
        )
    assert provider.requests == []
    assert control.task_spend["1"].calls == 0


def test_feature_budget_refusal_and_known_overage_stop_before_tools():
    refusal = WorkflowBudget(
        "f", _control(), limits=_limits(feature_tokens=1)
    )
    with pytest.raises(WorkflowBudgetExceeded, match=FEATURE_TOKEN_LIMIT):
        _reserve(refusal)

    control = _control()
    budget = WorkflowBudget("f", control, limits=_limits(feature_tokens=500))
    provider = MockLLMProvider([_response(total=1000)])
    events = []

    def dispatch(name, arguments):
        events.append((name, arguments))
        return ToolCallResult(status="committed", content={"ok": True}, committed=True)

    outcome = run_tool_loop(
        provider,
        budget,
        feature_id="f",
        stage="implement",
        scope="1",
        messages=[{"role": "user", "content": "work"}],
        tool_schemas=[{"type": "function", "function": {"name": "apply_patch"}}],
        dispatch=dispatch,
        max_rounds=2,
        max_calls_per_batch=2,
        output_cap=50,
    )
    assert outcome.overage is True
    assert outcome.exhausted is True
    assert events == []
    assert control.task_spend["1"].calls == 1


def test_task_debits_survive_reload_after_booking_without_settlement(tmp_path):
    store = JsonCheckpointStore(tmp_path)
    state = FeatureState(feature_id="f")
    control = _control(store)
    store.create_checkpoint(state, control)
    budget = WorkflowBudget(
        "f",
        control,
        limits=_limits(max_task_calls=1),
        persist=lambda updated: store.save_checkpoint(state, updated),
    )
    reservation = _reserve(budget)
    # Simulate a crash between booking and settlement.
    reloaded = store.load_checkpoint("f").control
    assert reloaded.task_spend["1"].calls == 1
    assert reloaded.task_spend["1"].tokens == reservation.charge
    second = WorkflowBudget("f", reloaded, limits=_limits(max_task_calls=1))
    with pytest.raises(WorkflowBudgetExceeded, match=TASK_CALL_LIMIT):
        _reserve(second)


def test_tool_loop_drops_ephemeral_reasoning_content():
    control = _control()
    budget = WorkflowBudget("f", control, limits=_limits())
    provider = MockLLMProvider(
        [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "private chain",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "read_range", "arguments": "{}"},
                        }
                    ],
                },
                "usage": TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                "usage_reported": True,
            },
            _response(),
        ]
    )

    def dispatch(name, arguments):
        return ToolCallResult(status="ok", content={"ok": True})

    outcome = run_tool_loop(
        provider,
        budget,
        feature_id="f",
        stage="implement",
        scope="1",
        messages=[{"role": "user", "content": "work"}],
        tool_schemas=[{"type": "function", "function": {"name": "read_range"}}],
        dispatch=dispatch,
        max_rounds=3,
        max_calls_per_batch=2,
        output_cap=50,
        thinking="enabled",
    )
    assert outcome.exhausted is False
    assert "private chain" not in str(outcome.events)
    replay = provider.requests[1]["messages"]
    assistant = [message for message in replay if message["role"] == "assistant"][0]
    assert assistant["reasoning_content"] == "private chain"
