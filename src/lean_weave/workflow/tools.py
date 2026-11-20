"""Typed, fixed-name tool bindings for the workflow.

Approval snapshots remain the only source of authority. Patch targets are
intersected with the active task files, inputs and results are bounded, and no
model-supplied command can reach a process. ``apply_patch`` durability
``unconfirmed`` is preserved as a committed-but-uncertain outcome.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lean_weave.contracts._types import ExecRequest, ExecResult
from lean_weave.errors import ForbiddenTool
from lean_weave.executor.approval import ToolApproval
from lean_weave.executor.models import (
    GUIDANCE,
    ApplyPatchRequest,
    ReadRangeRequest,
    ReadSymbolRequest,
    ToolFailure,
    normalize_tool_path,
)
from lean_weave.executor.patch import apply_patch
from lean_weave.executor.source import read_range, read_symbol
from lean_weave.logging_setup import get_logger
from lean_weave.verify.process import (
    IsolationBackend,
    ProcessSpec,
    bind_local,
    local_environment,
    run_bounded,
)

log = get_logger("workflow.tools")

MAX_TOOL_ARGUMENT_CHARS = 200_000
FIXED_TOOL_NAMES = frozenset({"read_range", "read_symbol", "apply_patch"})

BeforeEffect = Callable[[str, str | None, list[str]], None]
AfterEffect = Callable[[str], None]

TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": "read_range",
            "description": "Read an approved file range and return its revision.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["path", "start_line", "end_line"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_symbol",
            "description": "Read one Python symbol from an approved file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "name": {"type": "string"},
                },
                "required": ["path", "name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "Apply one exact unified diff to the current file revision.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "expected_revision": {"type": "string"},
                    "diff": {"type": "string"},
                },
                "required": ["path", "expected_revision", "diff"],
                "additionalProperties": False,
            },
        },
    },
)


@dataclass(frozen=True)
class ToolCallResult:
    status: str
    content: dict[str, Any]
    committed: bool = False
    unconfirmed: bool = False

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "committed", "committed_unconfirmed"}


def _failure(status: str) -> ToolCallResult:
    if status not in GUIDANCE or status == "ok":
        status = "invalid"
    return ToolCallResult(
        status=status,
        content={"ok": False, "status": status, "guidance": GUIDANCE[status]},
    )


def _bounded(content: dict[str, Any], limit: int) -> dict[str, Any]:
    try:
        encoded = json.dumps(content, separators=(",", ":"), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return {"ok": False, "status": "invalid", "guidance": GUIDANCE["invalid"]}
    if len(encoded) <= limit:
        return content
    return {"ok": False, "status": "oversized", "guidance": GUIDANCE["oversized"]}


class WorkflowTools:
    """One active task's read and patch surface."""

    def __init__(
        self,
        approval: ToolApproval,
        *,
        task_files: Sequence[str],
        checks_ready: bool,
        before_patch: BeforeEffect | None = None,
        after_patch: AfterEffect | None = None,
    ) -> None:
        self.approval = approval
        self._files = frozenset(normalize_tool_path(path) for path in task_files)
        self.checks_ready = checks_ready
        self._before_patch = before_patch
        self._after_patch = after_patch

    @property
    def task_files(self) -> frozenset[str]:
        return self._files

    def bind_task(self, files: Sequence[str], *, checks_ready: bool | None = None) -> "WorkflowTools":
        return WorkflowTools(
            self.approval,
            task_files=files,
            checks_ready=self.checks_ready if checks_ready is None else checks_ready,
            before_patch=self._before_patch,
            after_patch=self._after_patch,
        )

    def with_effects(
        self, before_patch: BeforeEffect | None, after_patch: AfterEffect | None
    ) -> "WorkflowTools":
        return WorkflowTools(
            self.approval,
            task_files=self._files,
            checks_ready=self.checks_ready,
            before_patch=before_patch,
            after_patch=after_patch,
        )

    def schemas(self) -> tuple[dict[str, Any], ...]:
        return TOOL_SCHEMAS

    def dispatch(self, name: str, arguments: str | Mapping[str, Any] | None) -> ToolCallResult:
        if name not in FIXED_TOOL_NAMES:
            return _failure("invalid")
        parsed = self._arguments(arguments)
        if parsed is None:
            return _failure("invalid")
        try:
            if name == "read_range":
                return self._read_range(parsed)
            if name == "read_symbol":
                return self._read_symbol(parsed)
            return self._apply_patch(parsed)
        except (TypeError, ValueError, KeyError):
            log.warning("[Workflow.tools] operation=%s outcome=invalid", name)
            return _failure("invalid")

    def _arguments(self, value: str | Mapping[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return {}
        if isinstance(value, Mapping):
            parsed = dict(value)
        elif isinstance(value, str):
            if len(value) > MAX_TOOL_ARGUMENT_CHARS:
                return None
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, RecursionError):
                return None
        else:
            return None
        if not isinstance(parsed, dict):
            return None
        return parsed

    def _scope(self, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        try:
            path = normalize_tool_path(value)
        except ValueError:
            return None
        if path not in self._files:
            log.warning("[Workflow.tools] outcome=out_of_scope")
            return None
        return path

    def _read_range(self, arguments: dict[str, Any]) -> ToolCallResult:
        path = self._scope(arguments.get("path"))
        if path is None:
            return _failure("denied")
        request = ReadRangeRequest(
            path=path,
            start_line=arguments.get("start_line"),
            end_line=arguments.get("end_line"),
        )
        result = read_range(self.approval, request)
        if isinstance(result, ToolFailure):
            return _failure(result.status)
        content = _bounded(
            result.model_dump(mode="json"),
            self.approval.limits.result_chars_max,
        )
        log.debug("[Workflow.tools] operation=read_range outcome=ok")
        return ToolCallResult(status="ok", content=content)

    def _read_symbol(self, arguments: dict[str, Any]) -> ToolCallResult:
        path = self._scope(arguments.get("path"))
        if path is None:
            return _failure("denied")
        request = ReadSymbolRequest(path=path, name=arguments.get("name"))
        result = read_symbol(self.approval, request)
        if isinstance(result, ToolFailure):
            return _failure(result.status)
        content = _bounded(
            result.model_dump(mode="json"),
            self.approval.limits.result_chars_max,
        )
        log.debug("[Workflow.tools] operation=read_symbol outcome=ok")
        return ToolCallResult(status="ok", content=content)

    def _apply_patch(self, arguments: dict[str, Any]) -> ToolCallResult:
        if not self.checks_ready:
            log.warning("[Workflow.tools] operation=apply_patch outcome=blocked")
            return _failure("blocked")
        path = self._scope(arguments.get("path"))
        if path is None:
            return _failure("denied")
        request = ApplyPatchRequest(
            path=path,
            expected_revision=arguments.get("expected_revision"),
            diff=arguments.get("diff"),
        )
        operation_id = f"patch-{uuid.uuid4().hex}"
        if self._before_patch is not None:
            try:
                self._before_patch(operation_id, request.expected_revision, sorted(self._files))
            except Exception:  # noqa: BLE001 - durable marker is a precondition
                log.warning("[Workflow.tools] operation=apply_patch outcome=marker_failed")
                return _failure("blocked")
        result = apply_patch(self.approval, request)
        if isinstance(result, ToolFailure):
            if self._after_patch is not None:
                try:
                    self._after_patch(operation_id)
                except Exception:  # noqa: BLE001 - no edit was committed
                    log.warning("[Workflow.tools] operation=apply_patch outcome=marker_clear_failed")
            log.debug("[Workflow.tools] operation=apply_patch outcome=%s", result.status)
            return _failure(result.status)
        if result.durability == "unconfirmed":
            # Keep the marker: the edit committed, but its durability is unknown.
            log.warning("[Workflow.tools] operation=apply_patch outcome=committed_unconfirmed")
            return ToolCallResult(
                status="committed_unconfirmed",
                content=result.model_dump(mode="json"),
                committed=True,
                unconfirmed=True,
            )
        if self._after_patch is not None:
            try:
                self._after_patch(operation_id)
            except Exception:  # noqa: BLE001 - keep the marker for a safe interrupt
                log.warning("[Workflow.tools] operation=apply_patch outcome=marker_clear_failed")
        log.info(
            "[Workflow.tools] operation=apply_patch outcome=committed added=%s removed=%s",
            result.lines_added,
            result.lines_removed,
        )
        return ToolCallResult(
            status="committed",
            content=result.model_dump(mode="json"),
            committed=True,
        )


class RegistryCodeExecutor:
    """``CodeExecutor`` adapter accepting only exact approved check matches."""

    def __init__(
        self,
        approval: ToolApproval,
        *,
        backend: IsolationBackend | None = None,
    ) -> None:
        self.approval = approval
        self.backend = backend

    def run(self, request: ExecRequest) -> ExecResult:
        if request.get("allow_network"):
            raise ForbiddenTool("Network execution is not authorized")
        argv = request.get("argv")
        if not isinstance(argv, Sequence) or isinstance(argv, (str, bytes)):
            raise ForbiddenTool("argv must be a sequence")
        argv_tuple = tuple(argv)
        check = next(
            (item for item in self.approval.checks if tuple(item.argv) == argv_tuple),
            None,
        )
        if check is None or not self.approval.checks_approved:
            raise ForbiddenTool("Command is not in the approved registry")
        cwd = request.get("cwd")
        if not isinstance(cwd, str):
            raise ForbiddenTool("A workspace cwd is required")
        try:
            resolved = Path(cwd).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            raise ForbiddenTool("Workspace cwd is invalid") from None
        if resolved != self.approval.root:
            raise ForbiddenTool("Command cwd is outside the approved workspace")
        timeout = request.get("timeout_s", check.timeout_s)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout <= 0
            or timeout > check.timeout_s
        ):
            raise ForbiddenTool("Command timeout is not authorized")
        cap = min(check.output_bytes_max, self.approval.limits.capture_bytes_max)
        if check.runs_workspace_code:
            if self.backend is None:
                raise ForbiddenTool("Workspace-code execution requires isolation")
            spec = ProcessSpec(
                argv=tuple(check.argv),
                cwd=str(self.approval.root),
                env=local_environment(),
                timeout_s=float(timeout),
                output_bytes_max=cap,
            )
            try:
                capture = self.backend.run(spec)
            except (OSError, RuntimeError, ValueError):
                raise ForbiddenTool("Isolated execution is unavailable") from None
        else:
            bound = bind_local(self.approval.root, check)
            if bound is None:
                raise ForbiddenTool("Command is not safe for local execution")
            capture = run_bounded(
                bound,
                cwd=self.approval.root,
                env=local_environment(),
                timeout_s=float(timeout),
                output_bytes_max=cap,
            )
        log.debug(
            "[Workflow.executor] check=%s outcome=%s bytes=%s",
            check.id,
            capture.status,
            len(capture.stdout) + len(capture.stderr),
        )
        return ExecResult(
            returncode=capture.exit_code if capture.exit_code is not None else 1,
            stdout=capture.stdout.decode("utf-8", "replace"),
            stderr=capture.stderr.decode("utf-8", "replace"),
            timed_out=capture.status == "timed_out",
        )
