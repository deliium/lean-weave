"""Pure plan request construction and strict plan policy validation.

Nothing here calls a provider, reads repository source, or grants authority.
Model output and caller-supplied single-task specs both become a validated
``FeaturePlan`` before the workflow writes anything.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict

from lean_weave.contracts._types import ContextBundle, LLMMessage, LLMResponse
from lean_weave.errors import PlanValidationError
from lean_weave.executor.models import normalize_tool_path
from lean_weave.logging_setup import get_logger
from lean_weave.schemas import (
    SCHEMA_VERSION,
    FeaturePlan,
    ImplementationTask,
    TaskStatus,
)
from lean_weave.workflow.instructions import as_system_block

log = get_logger("workflow.planning")

MAX_GOAL_CHARS = 2000
MAX_TASK_TITLE_CHARS = 200
MAX_TASK_DESCRIPTION_CHARS = 2000
MAX_TASKS = 32
MAX_FILES_PER_TASK = 16
MAX_DEPENDENCIES = 16
MAX_SETTINGS_CHARS = 2000
MAX_RECONCILIATION_ROWS = 16
MAX_CHECKPOINTS = 8

PLAN_RESPONSE_FORMAT: dict[str, str] = {"type": "json_object"}
PLAN_TOP_LEVEL_KEYS = frozenset(
    {"version", "goal", "settings", "reconciliation", "tasks", "commit_checkpoints"}
)
TASK_KEYS = frozenset(
    {"id", "title", "description", "files", "depends_on", "status", "outcome_digest"}
)
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DEFAULT_SETTINGS = {"testing": True, "logging": "verbose", "docs": True}


class SingleTaskSpec(BaseModel):
    """Caller-supplied compact spec for the explicit planning bypass."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: str
    title: str
    description: str = ""
    file: str
    check_ids: list[str]
    goal: str | None = None


@dataclass(frozen=True)
class PlanPolicy:
    """Caller-owned authorization and bounds for one plan validation."""

    approved_paths: frozenset[str] = frozenset()
    approved_check_ids: frozenset[str] = frozenset()
    max_tasks: int = MAX_TASKS
    bypass_allowed: bool = True
    require_bypass_checks: bool = True


@dataclass(frozen=True)
class PlanRequest:
    messages: list[LLMMessage]
    response_format: dict[str, str] = field(default_factory=lambda: dict(PLAN_RESPONSE_FORMAT))
    instruction_count: int = 0


def _fail(code: str) -> PlanValidationError:
    log.warning("[Plan.validate] outcome=rejected code=%s", code)
    return PlanValidationError(f"Plan rejected: {code}")


def _text(value: Any, *, what: str, max_chars: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _fail(f"invalid_{what}")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise _fail(f"invalid_{what}")
    if len(value) > max_chars:
        raise _fail(f"oversized_{what}")
    if not allow_empty and not value.strip():
        raise _fail(f"empty_{what}")
    return value


def _path(value: Any) -> str:
    if not isinstance(value, str):
        raise _fail("invalid_path")
    try:
        return normalize_tool_path(value)
    except ValueError:
        raise _fail("invalid_path") from None


def _settings(value: Any) -> dict[str, Any]:
    if value is None:
        return dict(_DEFAULT_SETTINGS)
    if not isinstance(value, Mapping):
        raise _fail("invalid_settings")
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key) is None:
            raise _fail("invalid_settings")
        if isinstance(item, bool) or isinstance(item, (str, int)):
            if isinstance(item, str) and len(item) > 200:
                raise _fail("oversized_settings")
            normalized[key] = item
        elif isinstance(item, list) and item and all(
            isinstance(entry, str) and len(entry) <= 200 for entry in item
        ):
            normalized[key] = list(item)
        else:
            raise _fail("invalid_settings")
    try:
        encoded = json.dumps(normalized, sort_keys=True)
    except (TypeError, ValueError):
        raise _fail("invalid_settings") from None
    if len(encoded) > MAX_SETTINGS_CHARS:
        raise _fail("oversized_settings")
    return normalized


def _reconciliation(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_RECONCILIATION_ROWS:
        raise _fail("invalid_reconciliation")
    rows: list[dict[str, str]] = []
    for row in value:
        if not isinstance(row, Mapping) or len(row) > 8:
            raise _fail("invalid_reconciliation")
        rendered: dict[str, str] = {}
        for key, item in row.items():
            if not isinstance(key, str) or not isinstance(item, str):
                raise _fail("invalid_reconciliation")
            rendered[key[:64]] = item[:300]
        rows.append(rendered)
    return rows


def _commit_checkpoints(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_CHECKPOINTS:
        raise _fail("invalid_checkpoints")
    total = 0
    for item in value:
        if not isinstance(item, Mapping) or not all(
            isinstance(entry, (str, int, bool)) or entry is None for entry in item.values()
        ):
            raise _fail("invalid_checkpoints")
        total += len(json.dumps(dict(item), default=str))
    if total > MAX_SETTINGS_CHARS:
        raise _fail("oversized_checkpoints")
    return [dict(item) for item in value]


def _tasks(value: Any, policy: PlanPolicy) -> list[ImplementationTask]:
    if not isinstance(value, list) or not value:
        raise _fail("empty_tasks")
    if len(value) > policy.max_tasks or len(value) > MAX_TASKS:
        raise _fail("too_many_tasks")
    tasks: list[ImplementationTask] = []
    ids: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise _fail("invalid_task")
        unknown = set(raw) - TASK_KEYS
        if unknown:
            raise _fail("unsupported_task_fields")
        identifier = raw.get("id")
        if not isinstance(identifier, str) or _TASK_ID.fullmatch(identifier) is None:
            raise _fail("invalid_task_id")
        if identifier in ids:
            raise _fail("duplicate_task_id")
        ids.add(identifier)
    for raw in value:
        identifier = raw["id"]
        title = _text(raw.get("title"), what="task_title", max_chars=MAX_TASK_TITLE_CHARS)
        description = _text(
            raw.get("description", ""),
            what="task_description",
            max_chars=MAX_TASK_DESCRIPTION_CHARS,
            allow_empty=True,
        )
        files_raw = raw.get("files", [])
        if not isinstance(files_raw, list) or len(files_raw) > MAX_FILES_PER_TASK:
            raise _fail("invalid_task_files")
        files: list[str] = []
        for entry in files_raw:
            path = _path(entry)
            if path not in policy.approved_paths:
                raise _fail("unapproved_task_path")
            if path not in files:
                files.append(path)
        depends_raw = raw.get("depends_on", [])
        if not isinstance(depends_raw, list) or len(depends_raw) > MAX_DEPENDENCIES:
            raise _fail("invalid_dependencies")
        depends: list[str] = []
        for entry in depends_raw:
            if not isinstance(entry, str) or entry not in ids:
                raise _fail("unknown_dependency")
            if entry == identifier:
                raise _fail("dependency_cycle")
            if entry not in depends:
                depends.append(entry)
        status_raw = raw.get("status", TaskStatus.PENDING.value)
        try:
            status = TaskStatus(status_raw)
        except ValueError:
            raise _fail("invalid_task_status") from None
        if status is not TaskStatus.PENDING:
            raise _fail("non_pending_task")
        if raw.get("outcome_digest") is not None:
            raise _fail("unexpected_outcome")
        tasks.append(
            ImplementationTask(
                id=identifier,
                title=title,
                description=description,
                files=files,
                depends_on=depends,
                status=TaskStatus.PENDING,
            )
        )
    _assert_acyclic(tasks)
    return tasks


def _assert_acyclic(tasks: Sequence[ImplementationTask]) -> None:
    graph = {task.id: list(task.depends_on) for task in tasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise _fail("dependency_cycle")
        visiting.add(node)
        for dependency in graph[node]:
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for task in tasks:
        visit(task.id)


def validate_plan_payload(payload: Any, policy: PlanPolicy) -> FeaturePlan:
    if not isinstance(payload, Mapping):
        raise _fail("plan_not_object")
    unknown = set(payload) - PLAN_TOP_LEVEL_KEYS
    if unknown:
        raise _fail("unsupported_plan_fields")
    version = payload.get("version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise _fail("unsupported_plan_version")
    goal = _text(payload.get("goal"), what="goal", max_chars=MAX_GOAL_CHARS)
    plan = FeaturePlan(
        goal=goal,
        settings=_settings(payload.get("settings")),
        reconciliation=_reconciliation(payload.get("reconciliation")),
        tasks=_tasks(payload.get("tasks"), policy),
        commit_checkpoints=_commit_checkpoints(payload.get("commit_checkpoints")),
    )
    log.debug(
        "[Plan.validate] outcome=ok source=generated tasks=%s files=%s",
        len(plan.tasks),
        sum(len(task.files) for task in plan.tasks),
    )
    return plan


def plan_output_contract(approved_paths: Sequence[str] = ()) -> str:
    """Stable JSON output contract shared by V1 and V2 plan requests."""

    paths = sorted({path for path in approved_paths if isinstance(path, str)})
    directive = [
        "Return exactly one JSON object with keys:",
        "goal, settings, reconciliation, tasks, commit_checkpoints.",
        "Each task object has id, title, description, files, depends_on; "
        "status must be omitted or pending.",
        "Use only these caller-approved existing files: " + (", ".join(paths) or "(none)"),
        "Do not include prose outside the JSON object.",
    ]
    return "PLAN OUTPUT CONTRACT:\n" + "\n".join(directive)


def build_plan_request(
    bundle: ContextBundle,
    *,
    instructions: Sequence[str] = (),
    approved_paths: Sequence[str] = (),
) -> PlanRequest:
    """Build the plan call from the canonical context message pair only."""

    messages = bundle.get("messages")
    if not isinstance(messages, list) or len(messages) != 2:
        raise _fail("missing_canonical_pair")
    system, user = messages
    if (
        not isinstance(system, Mapping)
        or system.get("role") != "system"
        or not isinstance(system.get("content"), str)
        or not isinstance(user, Mapping)
        or user.get("role") != "user"
        or not isinstance(user.get("content"), str)
    ):
        raise _fail("missing_canonical_pair")

    system_text = system["content"]
    block = as_system_block(instructions)
    if block:
        system_text = f"{system_text}\n\n{block}"
    user_text = user["content"]
    paths = sorted({path for path in approved_paths if isinstance(path, str)})
    user_text = f"{user_text}\n\n{plan_output_contract(paths)}"
    request = PlanRequest(
        messages=[
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ],
        instruction_count=len(instructions),
    )
    log.debug(
        "[Plan.request] outcome=ok instructions=%s approved_paths=%s",
        len(instructions),
        len(paths),
    )
    return request


def parse_plan_response(response: LLMResponse, policy: PlanPolicy) -> FeaturePlan:
    """Validate provider JSON or fail closed; never call the provider here."""

    structured = response.get("structured")
    if structured is not None:
        return validate_plan_payload(structured, policy)
    message = response.get("message") or {}
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, str) or not content.strip():
        raise _fail("missing_plan_json")
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, RecursionError):
        raise _fail("malformed_plan_json") from None
    return validate_plan_payload(payload, policy)


def normalize_single_task_spec(
    request: str,
    spec: SingleTaskSpec | Mapping[str, Any],
    policy: PlanPolicy,
) -> FeaturePlan:
    """Normalize one caller-supplied spec into a validated single-task plan."""

    if not policy.bypass_allowed:
        raise _fail("bypass_disabled")
    try:
        parsed = spec if isinstance(spec, SingleTaskSpec) else SingleTaskSpec.model_validate(spec)
    except (TypeError, ValueError):
        raise _fail("invalid_single_task_spec") from None
    if not policy.require_bypass_checks and not parsed.check_ids:
        log.debug("[Plan.bypass] checks=omitted")
    if policy.require_bypass_checks and not parsed.check_ids:
        raise _fail("missing_bypass_checks")
    if len(parsed.check_ids) != len(set(parsed.check_ids)):
        raise _fail("duplicate_bypass_checks")
    if any(check not in policy.approved_check_ids for check in parsed.check_ids):
        raise _fail("unapproved_bypass_check")
    path = _path(parsed.file)
    if path not in policy.approved_paths:
        raise _fail("unapproved_bypass_path")
    goal = parsed.goal if parsed.goal is not None else request
    payload = {
        "version": SCHEMA_VERSION,
        "goal": goal,
        "settings": dict(_DEFAULT_SETTINGS),
        "reconciliation": [
            {"decision": "explicit single-task bypass", "evidence": path}
        ],
        "tasks": [
            {
                "id": parsed.id,
                "title": parsed.title,
                "description": parsed.description,
                "files": [path],
                "depends_on": [],
                "status": TaskStatus.PENDING.value,
            }
        ],
        "commit_checkpoints": [],
    }
    plan = validate_plan_payload(payload, policy)
    log.info(
        "[Plan.bypass] outcome=ok feature_tasks=%s checks=%s",
        len(plan.tasks),
        len(parsed.check_ids),
    )
    return plan
