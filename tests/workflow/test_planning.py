import pytest

from lean_weave.errors import InstructionError, PlanValidationError
from lean_weave.workflow.instructions import load_instructions
from lean_weave.workflow.planning import (
    PlanPolicy,
    SingleTaskSpec,
    build_plan_request,
    normalize_single_task_spec,
    parse_plan_response,
)

POLICY = PlanPolicy(
    approved_paths=frozenset({"src/a.py", "src/b.py"}),
    approved_check_ids=frozenset({"parse", "tests"}),
)


def _bundle():
    return {
        "stage": "plan",
        "messages": [
            {"role": "system", "content": "system-policy"},
            {"role": "user", "content": "feature request"},
        ],
        "system_prompt": "system-policy",
        "user_prompt": "feature request",
    }


def _payload(**kwargs):
    base = {
        "version": "1.0.0",
        "goal": "add one helper",
        "settings": {"testing": True, "logging": "verbose", "docs": True},
        "reconciliation": [{"decision": "keep", "evidence": "src/a.py"}],
        "tasks": [
            {
                "id": "1",
                "title": "edit a",
                "description": "change a",
                "files": ["src/a.py"],
                "depends_on": [],
            }
        ],
        "commit_checkpoints": [],
    }
    base.update(kwargs)
    return base


def _response(payload=None, content=None):
    if content is not None:
        return {"message": {"role": "assistant", "content": content}}
    return {
        "message": {"role": "assistant", "content": None},
        "structured": _payload() if payload is None else payload,
    }


def test_instruction_registry_selects_only_registered_stage_fragments():
    selected = load_instructions("plan", ["plan.goal", "plan.tasks", "plan.goal"])
    assert len(selected) == 2
    assert load_instructions("verify")[0].startswith("Required approved checks")
    with pytest.raises(InstructionError):
        load_instructions("plan", ["implement.one_task"])
    with pytest.raises(InstructionError):
        load_instructions("plan", ["plan.unknown"])
    with pytest.raises(InstructionError):
        load_instructions("unknown")
    with pytest.raises(InstructionError):
        load_instructions("plan", ["plan.goal"] * 13)


def test_plan_request_uses_canonical_pair_without_mutation():
    bundle = _bundle()
    request = build_plan_request(
        bundle,
        instructions=load_instructions("plan"),
        approved_paths=["src/a.py"],
    )
    assert [message["role"] for message in request.messages] == ["system", "user"]
    assert "PROJECT METHOD FRAGMENTS" in request.messages[0]["content"]
    assert "src/a.py" in request.messages[1]["content"]
    assert "PLAN OUTPUT CONTRACT" in request.messages[1]["content"]
    assert bundle["messages"][0]["content"] == "system-policy"
    assert request.response_format == {"type": "json_object"}
    with pytest.raises(PlanValidationError):
        build_plan_request({"messages": [{"role": "user", "content": "x"}]})


def test_parse_valid_structured_and_text_plan_in_stable_order():
    first = parse_plan_response(_response(), POLICY)
    assert first.tasks[0].id == "1"
    text = parse_plan_response(_response(content='{"goal": "g", "tasks": [{"id": "2", "title": "t", "files": ["src/b.py"]}]}'), POLICY)
    assert text.tasks[0].files == ["src/b.py"]
    ordered = _payload(
        tasks=[
            {"id": "a", "title": "first", "files": ["src/a.py"], "depends_on": []},
            {"id": "b", "title": "second", "files": ["src/b.py"], "depends_on": ["a"]},
        ]
    )
    parsed = parse_plan_response(_response(ordered), POLICY)
    assert [task.id for task in parsed.tasks] == ["a", "b"]
    assert parsed.tasks[1].depends_on == ["a"]


@pytest.mark.parametrize(
    "payload",
    [
        {"goal": "g", "tasks": []},
        {"goal": "", "tasks": [{"id": "1", "title": "t"}]},
        {"goal": "g", "version": "2.0.0", "tasks": [{"id": "1", "title": "t"}]},
        {"goal": "g", "tasks": [{"id": "1", "title": "t"}, {"id": "1", "title": "u"}]},
        {"goal": "g", "tasks": [{"id": "1", "title": "t", "files": ["outside.py"]}]},
        {"goal": "g", "tasks": [{"id": "1", "title": "t", "depends_on": ["2"]}]},
        {"goal": "g", "tasks": [{"id": "1", "title": "t", "status": "done"}]},
        {"goal": "g", "tasks": [{"id": "1", "title": "t"}], "injected": True},
    ],
)
def test_invalid_generated_plans_fail_closed(payload):
    with pytest.raises(PlanValidationError):
        parse_plan_response(_response(payload), POLICY)


def test_dependency_cycles_and_forward_dependencies():
    cycle = _payload(
        tasks=[
            {"id": "a", "title": "a", "depends_on": ["b"]},
            {"id": "b", "title": "b", "depends_on": ["a"]},
        ]
    )
    with pytest.raises(PlanValidationError, match="dependency_cycle"):
        parse_plan_response(_response(cycle), POLICY)
    forward = _payload(
        tasks=[
            {"id": "a", "title": "a", "depends_on": ["b"]},
            {"id": "b", "title": "b", "depends_on": []},
        ]
    )
    parsed = parse_plan_response(_response(forward), POLICY)
    assert parsed.tasks[0].depends_on == ["b"]


def test_malformed_provider_output_is_rejected():
    with pytest.raises(PlanValidationError, match="missing_plan_json"):
        parse_plan_response({"message": {"role": "assistant", "content": "  "}}, POLICY)
    with pytest.raises(PlanValidationError, match="malformed_plan_json"):
        parse_plan_response({"message": {"role": "assistant", "content": "not-json"}}, POLICY)
    with pytest.raises(PlanValidationError, match="unsupported_plan_fields"):
        parse_plan_response(_response({"goal": "g", "tasks": [{"id": "1", "title": "t"}], "x": 1}), POLICY)


def test_settings_are_data_not_authority():
    payload = _payload(settings={"testing": True, "write_allow": ["src/a.py"], "docs": False})
    plan = parse_plan_response(_response(payload), POLICY)
    assert plan.settings["write_allow"] == ["src/a.py"]
    assert plan.tasks[0].files == ["src/a.py"]
    assert POLICY.approved_paths == frozenset({"src/a.py", "src/b.py"})


def test_single_task_bypass_requires_approved_scope_and_checks():
    spec = SingleTaskSpec(
        id="1",
        title="edit",
        description="change one line",
        file="src/a.py",
        check_ids=["parse"],
    )
    plan = normalize_single_task_spec("request", spec, POLICY)
    assert plan.goal == "request"
    assert plan.tasks[0].files == ["src/a.py"]
    with pytest.raises(PlanValidationError, match="unapproved_bypass_path"):
        normalize_single_task_spec(
            "request",
            spec.model_copy(update={"file": "outside.py"}),
            POLICY,
        )
    with pytest.raises(PlanValidationError, match="missing_bypass_checks"):
        normalize_single_task_spec("request", spec.model_copy(update={"check_ids": []}), POLICY)
    with pytest.raises(PlanValidationError, match="duplicate_bypass_checks"):
        normalize_single_task_spec(
            "request", spec.model_copy(update={"check_ids": ["parse", "parse"]}), POLICY
        )
    with pytest.raises(PlanValidationError, match="unapproved_bypass_check"):
        normalize_single_task_spec(
            "request", spec.model_copy(update={"check_ids": ["other"]}), POLICY
        )
    with pytest.raises(PlanValidationError, match="invalid_single_task_spec"):
        normalize_single_task_spec("request", {"id": 1, "title": "t"}, POLICY)
    disabled = PlanPolicy(
        approved_paths=POLICY.approved_paths,
        approved_check_ids=POLICY.approved_check_ids,
        bypass_allowed=False,
    )
    with pytest.raises(PlanValidationError, match="bypass_disabled"):
        normalize_single_task_spec("request", spec, disabled)
