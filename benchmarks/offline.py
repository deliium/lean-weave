"""Reusable offline CLI driver with scripted DeepSeek HTTP transport.

Injects ``httpx2.MockTransport`` at the gateway seam so reservations and
settlements use the real per-worktree journal. Not a product command.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import yaml
from typer.testing import CliRunner

from benchmarks.fixtures import (
    FixtureManifest,
    intended_edit_files,
    load_manifest,
    materialize,
    oracles_dir,
    resolve_check_argv,
)
from lean_weave.app import create_llm_provider
from lean_weave.cli.app import app
from lean_weave.executor.models import file_revision
from lean_weave.metrics.jsonl import JsonlMetricsCollector
from lean_weave.schemas import SCHEMA_VERSION
from lean_weave.workflow.orchestrator import MinimalWorkflowOrchestrator
from lean_weave.workflow.session import FeatureSession
from lean_weave.worktree.git import preflight_repository
from lean_weave.worktree.registry import read_routing

log = logging.getLogger("lean_weave.benchmarks.offline")

CompletionBuilder = Callable[[Mapping[str, Any]], dict[str, Any]]
Runner = CliRunner()


@dataclass
class ContentShadow:
    """Track bytes the scripted model believes are on disk."""

    files: dict[str, bytes] = field(default_factory=dict)

    @classmethod
    def from_tree(cls, root: Path) -> ContentShadow:
        files: dict[str, bytes] = {}
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if ".git" in path.parts or ".fixture-home" in path.parts:
                continue
            files[path.relative_to(root).as_posix()] = path.read_bytes()
        return cls(files=files)

    def revision(self, relative: str) -> str:
        return file_revision(self.files[relative])

    def text(self, relative: str) -> str:
        return self.files[relative].decode("utf-8")

    def set_text(self, relative: str, text: str) -> None:
        self.files[relative] = text.replace("\r\n", "\n").encode("utf-8")


def unified_replace_diff(path: str, old: str, new: str) -> str:
    """Build a whole-file unified diff with LF transport lines."""

    old_lines = old.splitlines()
    new_lines = new.splitlines()
    old_count = len(old_lines) if old_lines else 1
    new_count = len(new_lines) if new_lines else 1
    body: list[str] = []
    if old_lines:
        body.extend(f"-{line}" for line in old_lines)
    else:
        body.append("-")
        old_count = 1
    if new_lines:
        body.extend(f"+{line}" for line in new_lines)
    else:
        body.append("+")
        new_count = 1
    return (
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1,{old_count} +1,{new_count} @@\n"
        + "\n".join(body)
        + "\n"
    )


def chat_completion(
    *,
    content: str | None = None,
    tool_calls: Sequence[Mapping[str, Any]] | None = None,
    usage: Mapping[str, int] | None = None,
    model: str = "deepseek-flash",
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    finish = "stop"
    if tool_calls:
        message["content"] = None
        message["tool_calls"] = [dict(call) for call in tool_calls]
        finish = "tool_calls"
    tokens = dict(usage or {"prompt_tokens": 8, "completion_tokens": 6, "total_tokens": 14})
    return {
        "id": "chatcmpl-offline",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": message,
            }
        ],
        "usage": tokens,
    }


def plan_completion(
    manifest: FixtureManifest,
    *,
    tasks: Sequence[Mapping[str, Any]] | None = None,
    goal: str | None = None,
) -> dict[str, Any]:
    payload = {
        "version": SCHEMA_VERSION,
        "goal": goal or manifest.request,
        "settings": {"testing": True, "logging": "verbose", "docs": True},
        "reconciliation": [],
        "tasks": list(tasks) if tasks is not None else [dict(item) for item in manifest.task_plan],
        "commit_checkpoints": [],
    }
    return chat_completion(content=json.dumps(payload, separators=(",", ":")))


def apply_patch_call(
    shadow: ContentShadow,
    relative: str,
    new_text: str,
    *,
    call_id: str = "c1",
) -> dict[str, Any]:
    old = shadow.text(relative)
    revision = shadow.revision(relative)
    diff = unified_replace_diff(relative, old, new_text)
    shadow.set_text(relative, new_text)
    arguments = {
        "path": relative,
        "expected_revision": revision,
        "diff": diff,
    }
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "apply_patch",
            "arguments": json.dumps(arguments, separators=(",", ":")),
        },
    }


def tool_completion(
    shadow: ContentShadow,
    relative: str,
    new_text: str,
    *,
    call_id: str = "c1",
    usage: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    return chat_completion(
        tool_calls=[apply_patch_call(shadow, relative, new_text, call_id=call_id)],
        usage=usage,
    )


def final_completion(content: str = "done") -> dict[str, Any]:
    return chat_completion(content=content)


@dataclass
class ScriptedTransport:
    """Ordered DeepSeek chat-completion responses for MockTransport."""

    steps: list[CompletionBuilder | dict[str, Any]]
    requests: list[dict[str, Any]] = field(default_factory=list)

    @property
    def send_count(self) -> int:
        return len(self.requests)

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        try:
            body = json.loads(request.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            log.error("[Offline.transport] outcome=bad_request")
            return httpx2.Response(400, json={"error": {"message": "bad_request"}})
        self.requests.append(body)
        stage = _stage_hint(body)
        log.debug(
            "[Offline.transport] send=%s stage=%s remaining=%s",
            len(self.requests),
            stage,
            len(self.steps),
        )
        if not self.steps:
            log.error("[Offline.transport] outcome=script_exhausted")
            return httpx2.Response(500, json={"error": {"message": "script_exhausted"}})
        step = self.steps.pop(0)
        payload = step(body) if callable(step) else step
        return httpx2.Response(200, json=payload)


def _stage_hint(body: Mapping[str, Any]) -> str:
    if isinstance(body.get("response_format"), Mapping):
        return "plan"
    if body.get("tools"):
        return "tool"
    return "text"


def write_operator_config(
    path: Path,
    manifest: FixtureManifest,
    *,
    extra_workflow: Mapping[str, Any] | None = None,
    write_allow: Sequence[str] | None = None,
) -> Path:
    """Write a trusted external operator config for the fixture case."""

    checks: list[dict[str, Any]] = []
    for check in manifest.checks:
        argv = resolve_check_argv(check)
        checks.append(
            {
                "id": check.id,
                "executable": argv[0],
                "argv": argv,
                "timeout_s": check.timeout_s,
                "output_bytes_max": check.output_bytes_max,
                "runs_workspace_code": check.runs_workspace_code,
            }
        )
    workflow = dict(manifest.workflow_limits)
    if extra_workflow:
        workflow.update(dict(extra_workflow))
    payload: dict[str, Any] = {
        "version": 1,
        "llm": {
            "provider": "deepseek",
            "model": "deepseek-flash",
            "base_url": "https://api.deepseek.com",
            "thinking": False,
            "max_retries": 0,
            "max_output_tokens": 256,
        },
        "workflow": workflow,
        "tools": {
            "write_allow": list(write_allow) if write_allow is not None else list(manifest.write_paths),
            "checks": checks,
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    log.debug(
        "[Offline.config] path=%s checks=%s writes=%s",
        path.name,
        ",".join(manifest.check_ids),
        len(payload["tools"]["write_allow"]),
    )
    return path


def install_gateway_transport(
    monkeypatch: Any,
    transport: ScriptedTransport,
    *,
    api_key: str = "sk-offline-test",
) -> list[Any]:
    """Patch ``create_llm_provider`` so DeepSeek uses the scripted transport."""

    providers: list[Any] = []
    original = create_llm_provider

    def wrapped(context, **kwargs):
        kwargs.setdefault("transport", httpx2.MockTransport(transport.handler))
        kwargs.setdefault("api_key", api_key)
        provider = original(context, **kwargs)
        providers.append(provider)
        log.debug(
            "[Offline.gateway] provider=%s journal_id=%s",
            type(provider).__name__,
            id(getattr(provider, "_journal", None)),
        )
        return provider

    monkeypatch.setattr("lean_weave.app.create_llm_provider", wrapped)
    return providers


def bind_feature_session(monkeypatch: Any, origin: Path) -> None:
    """Pin CLI FeatureSession construction to the fixture origin checkout."""

    def factory(*args, **kwargs):
        kwargs.setdefault("source_cwd", origin)
        # Leave llm_provider unset so create_llm_provider builds DeepSeekProvider.
        return FeatureSession(*args, **kwargs)

    monkeypatch.setattr("lean_weave.cli.commands.feature.FeatureSession", factory)


def invoke_cli(*args: str, env: Mapping[str, str] | None = None):
    previous = {key: os.environ.get(key) for key in (env or {})}
    try:
        if env:
            os.environ.update(env)
        return Runner.invoke(app, list(args))
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def parse_view(stdout: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in values and key.startswith("plan_task"):
            continue
        values[key] = value
    return values


def approval_token(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("approve=") and "--approve " in line:
            return line.split("--approve ", 1)[1].strip()
    raise AssertionError("approval token missing")


def worktree_for(origin: Path, feature_id: str) -> Path:
    identity = preflight_repository(origin)
    record = read_routing(identity.common_dir, feature_id)
    return Path(record.worktree_root)


def journal_for(worktree: Path, state_dir: str = ".lean-weave") -> JsonlMetricsCollector:
    return JsonlMetricsCollector((worktree / state_dir).resolve())


def capture_orchestrators(monkeypatch: Any) -> list[Any]:
    """Record MinimalWorkflowOrchestrator instances for journal identity checks."""

    seen: list[Any] = []
    original = MinimalWorkflowOrchestrator.__init__

    def wrapped(self, *args, **kwargs):
        original(self, *args, **kwargs)
        seen.append(self)

    monkeypatch.setattr(MinimalWorkflowOrchestrator, "__init__", wrapped)
    return seen


def assert_shared_journal(providers: Sequence[Any], orchestrators: Sequence[Any]) -> Any:
    if not providers or not orchestrators:
        raise AssertionError("provider_or_orchestrator_missing")
    provider_journal = getattr(providers[0], "_journal", None)
    orchestrator_journal = getattr(orchestrators[0], "_journal", None)
    if provider_journal is None or orchestrator_journal is None:
        raise AssertionError("journal_missing")
    if provider_journal is not orchestrator_journal:
        raise AssertionError("journal_identity_mismatch")
    return provider_journal


def success_script(manifest: FixtureManifest, shadow: ContentShadow) -> list[Any]:
    """Plan → one patch per write path in task order → final text per task."""

    steps: list[Any] = [plan_completion(manifest)]
    edits = intended_edit_files(manifest.id)
    call_index = 0
    for task in manifest.task_plan:
        files = task.get("files") or []
        for relative in files:
            if relative not in edits:
                continue
            call_index += 1
            text = edits[relative]
            steps.append(
                tool_completion(shadow, relative, text, call_id=f"c{call_index}")
            )
            steps.append(final_completion(f"done-{task.get('id')}"))
    return steps


def review_plan_only_script(manifest: FixtureManifest) -> list[Any]:
    return [plan_completion(manifest)]


def out_of_policy_plan_script(manifest: FixtureManifest) -> list[Any]:
    tasks = [
        {
            "id": "1",
            "title": "escape",
            "description": "touch a denied path",
            "files": ["private/needed.py"],
            "depends_on": [],
        }
    ]
    return [plan_completion(manifest, tasks=tasks, goal="escape policy")]


def bad_dependency_plan_script(manifest: FixtureManifest) -> list[Any]:
    tasks = [
        {
            "id": "1",
            "title": "first",
            "description": "edit",
            "files": list(manifest.write_paths[:1]),
            "depends_on": ["missing"],
        }
    ]
    return [plan_completion(manifest, tasks=tasks)]


@dataclass(frozen=True)
class OfflineCase:
    manifest: FixtureManifest
    origin: Path
    config_path: Path
    shadow: ContentShadow


def prepare_case(
    case_id: str,
    tmp_path: Path,
    *,
    write_allow: Sequence[str] | None = None,
    extra_workflow: Mapping[str, Any] | None = None,
) -> OfflineCase:
    origin = materialize(case_id, tmp_path / "origin").root
    manifest = load_manifest(case_id)
    config_path = write_operator_config(
        tmp_path / "operator.yaml",
        manifest,
        write_allow=write_allow,
        extra_workflow=extra_workflow,
    )
    shadow = ContentShadow.from_tree(origin)
    log.debug("[Offline.prepare] id=%s head=%s", case_id, manifest.expected_head[:12])
    return OfflineCase(
        manifest=manifest,
        origin=origin,
        config_path=config_path,
        shadow=shadow,
    )


def installed_scripts_dir() -> Path:
    """Directory that holds sibling console scripts for this checkout's venv."""

    return Path(__file__).resolve().parent.parent / ".venv" / "bin"


def installed_entry(name: str = "wl") -> list[str]:
    """Prefer a named console script installed next to ``wl`` / ``lean-weave``."""

    if name not in {"wl", "lean-weave"}:
        raise ValueError(f"unsupported_entry:{name}")
    candidate = installed_scripts_dir() / name
    if candidate.is_file():
        return [str(candidate)]
    if name == "wl":
        return ["uv", "run", "--locked", "wl"]
    return ["uv", "run", "--locked", "lean-weave"]


def installed_wl() -> list[str]:
    """Prefer the console script installed into the local uv environment."""

    return installed_entry("wl")


def run_installed(
    *args: str,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    entry: str = "wl",
) -> subprocess.CompletedProcess[str]:
    merged = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(cwd),
        "LANG": "C",
        "LC_ALL": "C",
    }
    if env:
        merged.update(env)
    # Drop any live API key for offline executable smokes.
    merged.pop("DEEPSEEK_API_KEY", None)
    return subprocess.run(
        [*installed_entry(entry), *args],
        cwd=str(cwd),
        env=merged,
        check=False,
        capture_output=True,
        text=True,
    )


def oracle_path(name: str) -> Path:
    return oracles_dir() / name
