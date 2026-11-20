import json
import logging
import shutil
import sys
from pathlib import Path

import pytest

import lean_weave.workflow.tools as tools_module
from lean_weave.config.models import AppConfig, ApprovedCheck, ToolsConfig, WorkflowConfig
from lean_weave.context.provider import DeterministicContextProvider
from lean_weave.errors import ProviderError
from lean_weave.executor.approval import approve_tools
from lean_weave.executor.models import file_revision
from lean_weave.providers.mock import MockLLMProvider
from lean_weave.schemas import SCHEMA_VERSION, Stage, TokenUsage
from lean_weave.verify.runner import ApprovedChecksRunner
from lean_weave.workflow.orchestrator import MinimalWorkflowOrchestrator
from lean_weave.workflow.planning import PlanPolicy, SingleTaskSpec
from lean_weave.workflow.results import WorkflowStatus
from lean_weave.workflow.state import JsonCheckpointStore
from lean_weave.workflow.tools import WorkflowTools

REPO_FILE = Path("src/lean_weave/app.py")
FILE = REPO_FILE.as_posix()
ORIGINAL_LINE = 'log = get_logger("app")'
PARSER = """\
import ast
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
try:
    ast.parse(path.read_text(encoding="utf-8"))
except SyntaxError as exc:
    print(f"{sys.argv[1]}:{exc.lineno or 1}:1: E999 syntax error")
    raise SystemExit(1)
raise SystemExit(0)
"""


def _usage(total=10):
    return TokenUsage(prompt_tokens=4, completion_tokens=6, total_tokens=total)


def _final(content="done"):
    return {
        "message": {"role": "assistant", "content": content},
        "usage": _usage(),
        "usage_reported": True,
    }


def _plan_response():
    return {
        "message": {"role": "assistant", "content": None},
        "structured": {
            "version": SCHEMA_VERSION,
            "goal": "mark the copied module",
            "settings": {"testing": True, "logging": "verbose", "docs": True},
            "reconciliation": [],
            "tasks": [
                {
                    "id": "1",
                    "title": "edit the copied module",
                    "description": "add one marker comment",
                    "files": [FILE],
                    "depends_on": [],
                }
            ],
            "commit_checkpoints": [],
        },
        "usage": _usage(),
        "usage_reported": True,
    }


def _tool_response(arguments, call_id="c1"):
    return {
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "apply_patch",
                        "arguments": json.dumps(arguments),
                    },
                }
            ],
        },
        "usage": _usage(),
        "usage_reported": True,
    }


def _workspace(tmp_path, *, runs_workspace_code=False, max_retries=2, **workflow):
    root = tmp_path / "ws"
    destination = root / REPO_FILE
    destination.parent.mkdir(parents=True)
    shutil.copyfile(REPO_FILE, destination)
    parser = tmp_path / "parse_check.py"
    parser.write_text(PARSER, encoding="utf-8")
    check = ApprovedCheck(
        id="parse",
        argv=[sys.executable, str(parser), FILE],
        executable=sys.executable,
        timeout_s=10,
        output_bytes_max=4096,
        runs_workspace_code=runs_workspace_code,
    )
    config = AppConfig(
        workflow=WorkflowConfig(
            max_retries=max_retries,
            max_task_tokens=workflow.pop("max_task_tokens", 20_000),
            feature_token_budget=workflow.pop("feature_token_budget", 100_000),
            **workflow,
        ),
        tools=ToolsConfig(write_allow=[FILE], checks=[check]),
    )
    approval = approve_tools(root, config, allow_writes=True, allow_checks=True)
    return root, approval, config


def _build(root, approval, config, provider):
    store = JsonCheckpointStore(root)
    runner = ApprovedChecksRunner(approval, check_ids=["parse"])
    tools = WorkflowTools(approval, task_files=[], checks_ready=True)
    policy = PlanPolicy(
        approved_paths=frozenset({FILE}),
        approved_check_ids=frozenset({"parse"}),
    )
    orchestrator = MinimalWorkflowOrchestrator(
        config=config.workflow,
        llm_provider=provider,
        context_provider=DeterministicContextProvider(root),
        state_store=store,
        verification_runner=runner,
        tools=tools,
        plan_policy=policy,
        output_cap=1024,
    )
    return orchestrator, store


def _diff_text(source: str, old: str, new: str) -> dict:
    number = source.splitlines().index(old) + 1
    revision = file_revision(source.encode("utf-8"))
    diff = (
        f"--- a/{FILE}\n+++ b/{FILE}\n"
        f"@@ -{number},1 +{number},1 @@\n-{old}\n+{new}\n"
    )
    return {"path": FILE, "expected_revision": revision, "diff": diff}


def _diff(root: Path, old: str, new: str) -> tuple[str, dict]:
    text = (root / REPO_FILE).read_text(encoding="utf-8")
    source = text
    return file_revision(source.encode("utf-8")), _diff_text(source, old, new)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".lean-weave" not in path.relative_to(root).parts
    }


def _spec():
    return SingleTaskSpec(id="1", title="edit", file=FILE, check_ids=["parse"])


def test_detailed_plan_edit_and_verify_pass(tmp_path):
    root, approval, config = _workspace(tmp_path)
    revision, patch = _diff(root, ORIGINAL_LINE, ORIGINAL_LINE + "  # e2e")
    before = _snapshot(root)
    provider = MockLLMProvider(
        [_plan_response(), _tool_response(patch), _final()]
    )
    orchestrator, store = _build(root, approval, config, provider)

    state = orchestrator.run_feature("e2e", "mark the copied module")

    assert state.stage is Stage.DONE
    assert "# e2e" in (root / REPO_FILE).read_text(encoding="utf-8")
    after = _snapshot(root)
    changed = [path for path in after if after[path] != before[path]]
    assert changed == [FILE]
    assert before.keys() == after.keys()
    assert [request["stage"] for request in provider.requests] == [
        "plan",
        "implement",
        "implement",
    ]
    assert "Planning must not scan" not in str(provider.requests[1]["messages"])
    assert [message["role"] for message in provider.requests[1]["messages"]] == [
        "system",
        "user",
    ]
    result = orchestrator.result("e2e")
    assert result.status is WorkflowStatus.SUCCESS
    assert result.counters.prompt_tokens == 12
    assert result.counters.completion_tokens == 18
    assert result.counters.llm_calls == 3
    assert store.load_checkpoint("e2e").control.task_spend["1"].calls == 2


def test_bypass_path_makes_no_planning_call(tmp_path):
    root, approval, config = _workspace(tmp_path)
    before = _snapshot(root)
    provider = MockLLMProvider([_final()])
    orchestrator, _store = _build(root, approval, config, provider)

    state = orchestrator.run_simple("bypass", "no change needed", _spec())

    assert state.stage is Stage.DONE
    assert [request["stage"] for request in provider.requests] == ["implement"]
    assert _snapshot(root) == before
    assert orchestrator.result("bypass").status is WorkflowStatus.SUCCESS


def test_one_repair_then_pass_uses_compact_retry_context(tmp_path):
    root, approval, config = _workspace(tmp_path)
    broken = 'log = get_logger("app"'
    repaired = ORIGINAL_LINE + "  # repaired"
    _rev1, patch1 = _diff(root, ORIGINAL_LINE, broken)
    original_text = (root / REPO_FILE).read_text(encoding="utf-8")
    broken_text = original_text.replace(ORIGINAL_LINE, broken, 1)
    patch2 = _diff_text(broken_text, broken, repaired)
    provider = MockLLMProvider(
        [
            _plan_response(),
            _tool_response(patch1),
            _final(),
            _tool_response(patch2, call_id="c2"),
            _final("repaired"),
        ]
    )
    orchestrator, _store = _build(root, approval, config, provider)

    state = orchestrator.run_feature("repair", "mark the copied module")

    assert state.stage is Stage.DONE
    assert state.retry_count == 1
    assert "# repaired" in (root / REPO_FILE).read_text(encoding="utf-8")
    retry_requests = [
        request for request in provider.requests if request["stage"] == "retry"
    ]
    assert len(retry_requests) == 2
    rendered = str(retry_requests[0]["messages"])
    assert "E999" in rendered
    assert FILE in rendered
    assert "PLAN OUTPUT CONTRACT" not in rendered
    assert orchestrator.result("repair").status is WorkflowStatus.SUCCESS


def test_exhausted_retries_terminate_with_failed_status(tmp_path):
    root, approval, config = _workspace(tmp_path, max_retries=1)
    broken = 'log = get_logger("app"'
    _rev, patch = _diff(root, ORIGINAL_LINE, broken)
    provider = MockLLMProvider(
        [
            _plan_response(),
            _tool_response(patch),
            _final(),
            _final("no repair"),
        ]
    )
    orchestrator, _store = _build(root, approval, config, provider)

    state = orchestrator.run_feature("exhausted", "mark the copied module")

    assert state.stage is Stage.FAILED
    result = orchestrator.result("exhausted")
    assert result.status is WorkflowStatus.FAILED
    assert result.reason == "retry_exhausted"
    assert [request["stage"] for request in provider.requests] == [
        "plan",
        "implement",
        "implement",
        "retry",
    ]


def test_task_and_feature_budget_exhaustion_stop_before_dispatch(tmp_path):
    root, approval, config = _workspace(tmp_path, max_task_tokens=1)
    provider = MockLLMProvider([_final()])
    orchestrator, _store = _build(root, approval, config, provider)
    state = orchestrator.run_simple("task-budget", "edit", _spec())
    assert state.stage is Stage.FAILED
    assert provider.requests == []
    result = orchestrator.result("task-budget")
    assert result.status is WorkflowStatus.BUDGET_EXHAUSTED
    assert result.reason == "task_token_limit"

    root2, approval2, config2 = _workspace(tmp_path / "feature", feature_token_budget=1)
    provider2 = MockLLMProvider([_final()])
    orchestrator2, _store2 = _build(root2, approval2, config2, provider2)
    state2 = orchestrator2.run_simple("feature-budget", "edit", _spec())
    assert state2.stage is Stage.FAILED
    assert provider2.requests == []
    result2 = orchestrator2.result("feature-budget")
    assert result2.status is WorkflowStatus.BUDGET_EXHAUSTED
    assert result2.reason == "feature_token_limit"


def test_process_restart_recovery_and_result_lookup(tmp_path):
    root, approval, config = _workspace(tmp_path)
    first_provider = MockLLMProvider([ProviderError("transport failed")])
    first, _store = _build(root, approval, config, first_provider)
    interrupted = first.run_simple("restart", "edit", _spec())
    assert interrupted.stage is Stage.INTERRUPTED
    assert first.result("restart").status is WorkflowStatus.INTERRUPTED
    assert first.result("restart").reason == "provider_error"

    second_provider = MockLLMProvider([_final()])
    second, _store2 = _build(root, approval, config, second_provider)
    resumed = second.resume("restart")
    assert resumed.stage is Stage.DONE
    assert len(second_provider.requests) == 1
    result = second.result("restart")
    assert result.status is WorkflowStatus.SUCCESS
    assert result.reason == "verification_passed"


def test_committed_unconfirmed_patch_interrupts_without_replay(tmp_path, monkeypatch):
    root, approval, config = _workspace(tmp_path)
    _revision, patch = _diff(root, ORIGINAL_LINE, ORIGINAL_LINE + "  # applied")
    provider = MockLLMProvider([_tool_response(patch)])
    original = tools_module.apply_patch

    def unconfirmed(tool_approval, request):
        result = original(tool_approval, request)
        if hasattr(result, "durability"):
            return result.model_copy(update={"durability": "unconfirmed"})
        return result

    monkeypatch.setattr(tools_module, "apply_patch", unconfirmed)
    orchestrator, store = _build(root, approval, config, provider)

    state = orchestrator.run_simple("unconfirmed", "edit", _spec())

    assert state.stage is Stage.INTERRUPTED
    assert state.interrupt["reason"] == "committed_unconfirmed"
    assert "# applied" in (root / REPO_FILE).read_text(encoding="utf-8")
    envelope = store.load_checkpoint("unconfirmed")
    assert envelope.control.pending_effect is not None
    assert envelope.control.pending_effect.confirmed is False

    resume_provider = MockLLMProvider([])
    resumed_orchestrator, _store2 = _build(root, approval, config, resume_provider)
    resumed = resumed_orchestrator.resume("unconfirmed")
    assert resumed.stage is Stage.INTERRUPTED
    assert resume_provider.requests == []
    assert resumed_orchestrator.result("unconfirmed").reason == "unresolved_effect"


def test_workspace_code_check_without_backend_refuses_before_edits(tmp_path):
    root, approval, config = _workspace(tmp_path, runs_workspace_code=True)
    provider = MockLLMProvider([_final()])
    orchestrator, _store = _build(root, approval, config, provider)
    before = _snapshot(root)

    state = orchestrator.run_simple("no-backend", "edit", _spec())

    assert state.stage is Stage.FAILED
    assert provider.requests == []
    assert _snapshot(root) == before
    result = orchestrator.result("no-backend")
    assert result.status is WorkflowStatus.FAILED
    assert result.reason == "isolation_unavailable"


def test_logs_are_metadata_only(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    root, approval, config = _workspace(tmp_path)
    canary = "CANARY_PROMPT_TEXT_123"
    provider = MockLLMProvider([_final()])
    orchestrator, _store = _build(root, approval, config, provider)

    state = orchestrator.run_simple("logs", canary, _spec())

    assert state.stage is Stage.DONE
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert canary not in rendered
    assert "PLAN OUTPUT CONTRACT" not in rendered
    assert "expected_revision" not in rendered
