"""Run an approved check batch and return compact diagnostics.

The batch is preflighted before any process starts. An empty registry, an
unknown id, a duplicate, or a blocked entry is not a pass. Diagnostic
summaries come from fixed templates; child output is never copied into the
result.
"""

from __future__ import annotations

import re
import time

from lean_weave.context.policy import contains_secret
from lean_weave.executor.approval import ToolApproval
from lean_weave.executor.models import GUIDANCE, normalize_tool_path, parse_model
from lean_weave.executor.workspace import Workspace
from lean_weave.logging_setup import get_logger
from lean_weave.verify.models import (
    CheckDiagnostic,
    CheckItem,
    RunChecksRequest,
    RunChecksResult,
    checks_failure,
)
from lean_weave.verify.process import IsolationBackend, ProcessSpec, bind_local, local_environment, run_bounded

log = get_logger("verify.checks")

_DIAG = re.compile(
    r"^(?P<path>[A-Za-z0-9_./-]+):(?P<line>[1-9][0-9]*):(?:(?P<col>[1-9][0-9]*):)?\s*(?P<code>[A-Z][A-Z0-9_]{0,31})\b"
)
_TEMPLATES = {
    "E999": "Python syntax error",
    "F401": "Unused import",
    "F841": "Unused variable",
    "E501": "Line too long",
    "PY001": "Python parse error",
    "TIMEOUT": "The check exceeded its deadline",
    "OUTPUT_LIMIT": "The check exceeded its output budget",
    "SPAWN": "The check could not be started",
    "UNRECOGNIZED": "Unrecognized diagnostic output",
}


def run_checks(
    approval: ToolApproval,
    request: RunChecksRequest | dict | None = None,
    *,
    backend: IsolationBackend | None = None,
) -> RunChecksResult:
    parsed = parse_model(RunChecksRequest, {} if request is None else request)
    if not isinstance(parsed, RunChecksRequest):
        return checks_failure("invalid")
    if not approval.checks_approved:
        log.warning("[Tools.checks] outcome=no_checks")
        return checks_failure("no_checks")
    registry = {item.id: item for item in approval.checks}
    if parsed.check_ids is None:
        selected = list(approval.checks)
        if not selected:
            log.warning("[Tools.checks] outcome=no_checks")
            return checks_failure("no_checks")
    else:
        if not parsed.check_ids:
            return checks_failure("invalid")
        if len(parsed.check_ids) != len(set(parsed.check_ids)):
            return checks_failure("duplicate_check")
        if any(item not in registry for item in parsed.check_ids):
            return checks_failure("unknown_check")
        selected = [registry[item] for item in parsed.check_ids]
    if len(selected) > approval.limits.checks_max:
        return checks_failure("invalid")

    plans: list[tuple[str, object]] = []
    blocked = False
    for check in selected:
        if check.runs_workspace_code:
            if backend is None:
                blocked = True
                plans.append(("blocked", check))
            else:
                plans.append(("isolated", check))
            continue
        bound = bind_local(approval.root, check)
        if bound is None:
            blocked = True
            plans.append(("blocked", check))
        else:
            plans.append(("local", check, bound))
    if blocked:
        items = []
        for plan in plans:
            check = plan[1]
            status = "blocked" if plan[0] == "blocked" else "not_run"
            items.append(CheckItem(check_id=check.id, status=status, diagnostics=_fixed(status)))
        log.warning("[Tools.checks] outcome=blocked")
        return RunChecksResult(ok=False, status="blocked", guidance=GUIDANCE["blocked"], items=items)

    workspace = Workspace(approval)
    workspace.begin()
    items: list[CheckItem] = []
    started = time.monotonic()
    captured = 0
    for index, plan in enumerate(plans):
        remaining = approval.limits.runtime_s_max - (time.monotonic() - started)
        if remaining <= 0.01:
            items.extend(CheckItem(check_id=plans[later][1].id, status="not_run") for later in range(index, len(plans)))
            break
        check = plan[1]
        timeout = min(check.timeout_s, remaining)
        cap = min(check.output_bytes_max, approval.limits.capture_bytes_max - captured)
        if cap <= 0:
            items.append(CheckItem(check_id=check.id, status="output_limited", diagnostics=_fixed("output_limited")))
            items.extend(CheckItem(check_id=plans[later][1].id, status="not_run") for later in range(index + 1, len(plans)))
            break
        if plan[0] == "local":
            capture = run_bounded(
                plan[2],
                cwd=approval.root,
                env=local_environment(),
                timeout_s=timeout,
                output_bytes_max=cap,
            )
        else:
            spec = ProcessSpec(
                argv=tuple(check.argv),
                cwd=str(approval.root),
                env=local_environment(),
                timeout_s=timeout,
                output_bytes_max=cap,
            )
            try:
                capture = backend.run(spec)
            except (OSError, RuntimeError, ValueError):
                capture = None
        if capture is None or capture.status == "spawn_error":
            items.append(CheckItem(check_id=check.id, status="failed", diagnostics=_fixed("spawn")))
            continue
        captured += len(capture.stdout) + len(capture.stderr)
        if capture.status == "timed_out":
            items.append(CheckItem(check_id=check.id, status="timed_out", diagnostics=_fixed("timed_out")))
            continue
        if capture.status == "output_limited":
            items.append(CheckItem(check_id=check.id, status="output_limited", diagnostics=_fixed("output_limited")))
            continue
        diagnostics, omitted = _diagnostics(capture.stdout + b"\n" + capture.stderr, workspace)
        status = "passed" if capture.exit_code == 0 else "failed"
        items.append(
            CheckItem(
                check_id=check.id,
                status=status,
                exit_code=capture.exit_code,
                diagnostics=diagnostics,
                diagnostic_omissions=omitted,
            )
        )
    else:
        items = items

    passed = sum(item.status == "passed" for item in items)
    if items and all(item.status == "passed" for item in items):
        outcome = "passed"
        ok = True
    else:
        outcome = "failed"
        ok = False
    log.info("[Tools.checks] outcome=%s passed=%s failed=%s", outcome, passed, len(items) - passed)
    log.debug("[Tools.checks] diagnostics=%s", sum(len(item.diagnostics) for item in items))
    if outcome != "passed":
        log.warning("[Tools.checks] outcome=%s", outcome)
    return RunChecksResult(ok=ok, status=outcome, guidance=GUIDANCE.get(outcome, GUIDANCE["failed"]), items=items)


def _fixed(status: str) -> list[CheckDiagnostic]:
    code = {
        "timed_out": "TIMEOUT",
        "output_limited": "OUTPUT_LIMIT",
        "spawn": "SPAWN",
        "blocked": "SPAWN",
    }.get(status)
    if code is None:
        return []
    return [CheckDiagnostic(code=code, summary=_TEMPLATES[code])]


def _diagnostics(payload: bytes, workspace: Workspace) -> tuple[list[CheckDiagnostic], int]:
    try:
        text = payload.decode("utf-8", "replace")
    except UnicodeError:
        text = ""
    found: list[CheckDiagnostic] = []
    seen: set[tuple[str, str, int, str]] = set()
    omitted = 0
    unrecognized = False
    for raw in text.splitlines():
        if any(ord(char) < 32 and char not in "\t" for char in raw):
            unrecognized = True
            continue
        match = _DIAG.match(raw.strip())
        if match is None:
            if raw.strip():
                unrecognized = True
            continue
        code = match.group("code")
        summary = _TEMPLATES.get(code, _TEMPLATES["UNRECOGNIZED"])
        path = match.group("path")
        line = int(match.group("line"))
        safe_path = _authorize_location(workspace, path)
        if contains_secret(code) or contains_secret(summary) or (path and contains_secret(path)):
            safe_path = None
        key = (code, safe_path or "", line if safe_path else 0, summary)
        if key in seen:
            omitted += 1
            continue
        if len(found) >= workspace.approval.limits.diagnostics_max:
            omitted += 1
            continue
        seen.add(key)
        found.append(CheckDiagnostic(code=code, summary=summary, path=safe_path, line=line if safe_path else None))
    if unrecognized:
        key = ("UNRECOGNIZED", "", 0, _TEMPLATES["UNRECOGNIZED"])
        if key not in seen and len(found) < workspace.approval.limits.diagnostics_max:
            found.append(CheckDiagnostic(code="UNRECOGNIZED", summary=_TEMPLATES["UNRECOGNIZED"]))
        elif key in seen or len(found) >= workspace.approval.limits.diagnostics_max:
            omitted += 1
    return found, omitted


def _authorize_location(workspace: Workspace, path: str) -> str | None:
    try:
        relative = normalize_tool_path(path)
    except ValueError:
        return None
    decision = workspace.metadata_decision(relative)
    if decision != "ok":
        return None
    return relative
