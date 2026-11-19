"""Read-only staged and unstaged Git inspection.

Only a real ``.git`` directory at the workspace root is accepted. A missing
``.git`` is ``unavailable`` and does not invoke Git. A symlink or gitfile is
``unavailable`` as well, including a dangling link, and is not treated as an
absent repository that should be searched for in a parent directory.

Results are metadata only: path and status, no patch bodies. Rename detection
is disabled. Paths that fail authorization are omitted without counts. A
comparison that changed only unauthorized paths is ``filtered``, not ``clean``.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from lean_weave.context.git_metadata import _UNSAFE_CONFIG, _environment
from lean_weave.context.policy import contains_secret
from lean_weave.executor.approval import ToolApproval
from lean_weave.executor.models import (
    GUIDANCE,
    GitDiffSuccess,
    GitPathEntry,
    InspectGitDiffRequest,
    ToolFailure,
    cap_result_items,
    failure,
    normalize_tool_path,
    parse_model,
)
from lean_weave.executor.workspace import Workspace
from lean_weave.logging_setup import get_logger
from lean_weave.verify.process import run_bounded

log = get_logger("executor.git")

_ALLOWED = {"A", "M", "D", "T"}


def inspect_git_diff(
    approval: ToolApproval, request: InspectGitDiffRequest | dict
) -> GitDiffSuccess | ToolFailure:
    parsed = parse_model(InspectGitDiffRequest, request)
    if isinstance(parsed, ToolFailure):
        return parsed
    workspace = Workspace(approval)
    workspace.begin()
    if parsed.paths:
        for path in parsed.paths:
            decision = workspace.metadata_decision(path)
            if decision != "ok":
                log.debug("[Tools.git] mode=%s outcome=denied", parsed.mode)
                return failure("denied")
    kind = _repository_kind(approval.root)
    if kind != "directory":
        log.debug("[Tools.git] mode=%s outcome=unavailable reason=%s", parsed.mode, kind)
        return _git_result("unavailable")
    config_status = _config_status(approval)
    if config_status == "incomplete":
        log.warning("[Tools.git] outcome=incomplete")
        return _git_result("incomplete")
    if config_status != "safe":
        log.warning("[Tools.git] outcome=unavailable")
        return _git_result("unavailable")
    top_status, top_code, top_data = _git(approval, ["rev-parse", "--show-toplevel"])
    interrupted = _interrupted(top_status)
    if interrupted is not None:
        return interrupted
    if top_status != "exited" or top_code != 0 or not _same_root(approval.root, top_data):
        log.warning("[Tools.git] outcome=unavailable")
        return _git_result("unavailable")
    head_status, head_code, _head_data = _git(approval, ["rev-parse", "--verify", "--quiet", "HEAD"])
    interrupted = _interrupted(head_status)
    if interrupted is not None:
        return interrupted
    if parsed.mode == "staged" and (head_status != "exited" or head_code != 0):
        log.debug("[Tools.git] mode=staged outcome=missing_baseline")
        return _git_result("missing_baseline")
    args = [
        "diff",
        "--name-status",
        "-z",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
    ]
    if parsed.mode == "staged":
        args.insert(1, "--cached")
    diff_status, code, data = _git(approval, args)
    interrupted = _interrupted(diff_status)
    if interrupted is not None:
        log.warning("[Tools.git] mode=%s outcome=incomplete", parsed.mode)
        return interrupted
    if diff_status != "exited" or code not in {0, 1}:
        log.warning("[Tools.git] mode=%s outcome=unavailable", parsed.mode)
        return _git_result("unavailable")
    parsed_entries = _parse_name_status(data)
    if parsed_entries is None:
        log.warning("[Tools.git] mode=%s outcome=unavailable", parsed.mode)
        return _git_result("unavailable")
    if parsed.paths:
        selected = set(parsed.paths)
        parsed_entries = [item for item in parsed_entries if item[1] in selected]
    if not parsed_entries:
        log.debug("[Tools.git] mode=%s outcome=clean count=0", parsed.mode)
        return _git_result("clean")
    visible, filtered, indeterminate = _admit(workspace, parsed.mode, parsed_entries)
    if indeterminate:
        log.warning("[Tools.git] mode=%s outcome=incomplete", parsed.mode)
        return _git_result("incomplete")
    if not visible and filtered:
        log.debug("[Tools.git] mode=%s outcome=filtered", parsed.mode)
        return _git_result("filtered")
    entries = [GitPathEntry(path=path, status=status) for status, path in sorted(visible, key=lambda item: item[1])]
    log.debug("[Tools.git] mode=%s outcome=changes count=%s", parsed.mode, len(entries))
    result = GitDiffSuccess(ok=True, status="changes", guidance=GUIDANCE["changes"], entries=entries)
    return cap_result_items(result, "entries", approval.limits.result_chars_max)


def _git_result(status: str) -> GitDiffSuccess:
    ok = status in {"clean", "changes", "filtered"}
    return GitDiffSuccess(ok=ok, status=status, guidance=GUIDANCE[status], entries=[])


def _config_status(approval: ToolApproval) -> str:
    """Check repository config with the approval's shared subprocess budget."""

    timeout = min(5.0, approval.limits.runtime_s_max)
    cap = approval.limits.capture_bytes_max
    capture = run_bounded(
        [
            "git",
            "-C",
            str(approval.root),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            "core.preloadindex=false",
            "-c",
            "core.hooksPath=",
            "config",
            "--get-regexp",
            r"^(filter\..*\.(clean|smudge|process)|diff\..*\.(command|textconv)|core\.fsmonitor)$",
        ],
        cwd=approval.root,
        env=_environment(),
        timeout_s=timeout,
        output_bytes_max=cap,
    )
    log.debug("[FIX] git config budget timeout_s=%s capture_bytes=%s status=%s", timeout, cap, capture.status)
    if capture.status in {"output_limited", "timed_out"}:
        return "incomplete"
    if capture.status != "exited":
        return "unavailable"
    if capture.exit_code == 1 and not capture.stdout.strip():
        return "safe"
    if capture.exit_code != 0:
        return "unavailable"
    text = capture.stdout.decode("utf-8", "replace")
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 1)
        key = parts[0]
        value = parts[1].strip().lower() if len(parts) > 1 else ""
        if key.lower() == "core.fsmonitor" and value in {"", "false", "0", "off"}:
            continue
        if _UNSAFE_CONFIG.match(key):
            return "unsafe"
    return "safe"


def _repository_kind(root: Path) -> str:
    candidate = root / ".git"
    try:
        info = os.lstat(candidate)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unsupported"
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if not stat.S_ISDIR(info.st_mode):
        return "gitfile"
    return "directory"


def _same_root(root: Path, data: bytes) -> bool:
    try:
        text = data.decode("utf-8").strip()
        return Path(text).resolve() == root.resolve()
    except (OSError, UnicodeError, RuntimeError, ValueError):
        return False


def _interrupted(status: str) -> GitDiffSuccess | None:
    if status in {"output_limited", "timed_out"}:
        log.warning("[Tools.git] outcome=incomplete")
        return _git_result("incomplete")
    return None


def _git(approval: ToolApproval, args: list[str]) -> tuple[str, int | None, bytes]:
    capture = run_bounded(
        ["git", "-C", str(approval.root), "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
         "-c", "core.preloadindex=false", "-c", "core.hooksPath=", "-c", "diff.renames=false", *args],
        cwd=approval.root,
        env=_environment(),
        timeout_s=min(5.0, approval.limits.runtime_s_max),
        output_bytes_max=approval.limits.capture_bytes_max,
    )
    if capture.status != "exited" or capture.exit_code is None:
        return capture.status, None, b""
    return "exited", capture.exit_code, capture.stdout


def _parse_name_status(data: bytes) -> list[tuple[str, str]] | None:
    if data.endswith(b"\0"):
        data = data[:-1]
    if not data:
        return []
    parts = data.split(b"\0")
    if len(parts) % 2:
        return None
    entries: list[tuple[str, str]] = []
    for index in range(0, len(parts), 2):
        try:
            status = parts[index].decode("utf-8")
            path = parts[index + 1].decode("utf-8")
        except UnicodeError:
            return None
        if status not in _ALLOWED or not _safe_path(path):
            return None
        entries.append((status, path))
    return entries


def _safe_path(path: str) -> bool:
    if not path or path.startswith("/") or "\\" in path or any(ord(char) < 32 for char in path):
        return False
    parts = path.split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def _admit(workspace: Workspace, mode: str, entries: list[tuple[str, str]]):
    visible: list[tuple[str, str]] = []
    filtered = False
    for status, path in entries:
        try:
            normalize_tool_path(path)
        except ValueError:
            return [], False, True
        if contains_secret(path):
            filtered = True
            continue
        decision = workspace.metadata_decision(path)
        if decision == "indeterminate":
            return [], False, True
        if decision != "ok":
            filtered = True
            continue
        if status != "D":
            loaded = workspace.read(path)
            if isinstance(loaded, str):
                if loaded in {"indeterminate", "scan_limit", "unreadable"}:
                    log.debug("[FIX] git admission incomplete code=%s", loaded)
                    return [], False, True
                if loaded == "secret":
                    filtered = True
                    continue
                visible.append((status, path))
                continue
        visible.append((status, path))
    return visible, filtered, False
