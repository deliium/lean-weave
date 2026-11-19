"""Bounded subprocess capture for approved checks.

A direct local backend may run only operator-approved commands that do not
load workspace code. Commands that execute workspace code require a trusted
injected backend which promises offline execution, a read-only workspace view,
and separate scratch space. This module does not implement a new sandbox and
never falls back to an unsandboxed run.

Process-group cleanup sends ``SIGKILL`` to the child's session. A descendant
that has already left the session can survive; that limitation is reported
rather than hidden by a second, weaker execution path.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from lean_weave.config.models import ApprovedCheck
from lean_weave.logging_setup import get_logger

log = get_logger("verify.process")


@dataclass(frozen=True)
class ProcessSpec:
    argv: tuple[str, ...]
    cwd: str
    env: Mapping[str, str]
    timeout_s: float
    output_bytes_max: int
    offline: bool = True
    read_only_workspace: bool = True


@dataclass(frozen=True)
class ProcessCapture:
    status: str
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    duration_ms: int


class IsolationBackend(Protocol):
    def run(self, spec: ProcessSpec) -> ProcessCapture:
        """Run a workspace-code check under offline, read-only isolation."""


def local_environment() -> dict[str, str]:
    """Minimal environment. Inherited credentials and search paths are dropped."""

    return {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }


def bind_local(root: Path, check: ApprovedCheck) -> tuple[str, ...] | None:
    """Bind argv to an absolute executable outside ``root``.

    Returns None when the check is not safe for the direct local backend.
    """

    if check.runs_workspace_code:
        return None
    if not check.argv or check.argv[0] != check.executable:
        return None
    executable = Path(check.executable)
    if not executable.is_absolute():
        return None
    try:
        resolved = executable.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    try:
        if resolved.is_relative_to(root.resolve()):
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    if not resolved.is_file():
        return None
    bound: list[str] = [str(resolved)]
    for arg in check.argv[1:]:
        if arg.startswith("/") or (len(arg) > 2 and arg[1] == ":"):
            try:
                arg_path = Path(arg).resolve()
            except (OSError, RuntimeError):
                return None
            try:
                if arg_path.is_relative_to(root.resolve()):
                    return None
            except (OSError, RuntimeError, ValueError):
                return None
        bound.append(arg)
    return tuple(bound)


def run_bounded(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_s: float,
    output_bytes_max: int,
) -> ProcessCapture:
    """Capture stdout and stderr with a deadline and a combined byte cap."""

    started = time.monotonic()
    if not argv or timeout_s <= 0 or output_bytes_max <= 0:
        return ProcessCapture("spawn_error", None, b"", b"", 0)
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            shell=False,
        )
    except (OSError, ValueError):
        log.warning("[Tools.process] outcome=spawn_error")
        return ProcessCapture("spawn_error", None, b"", b"", _elapsed(started))

    stdout_parts: list[bytes] = []
    stderr_parts: list[bytes] = []
    overflow = threading.Event()

    def _drain(pipe, parts: list[bytes]) -> None:
        if pipe is None:
            return
        try:
            while not overflow.is_set():
                chunk = pipe.read(8192)
                if not chunk:
                    break
                parts.append(chunk)
                if sum(len(item) for item in stdout_parts) + sum(len(item) for item in stderr_parts) > output_bytes_max:
                    overflow.set()
                    break
        except OSError:
            overflow.set()

    threads = (
        threading.Thread(target=_drain, args=(process.stdout, stdout_parts), daemon=True),
        threading.Thread(target=_drain, args=(process.stderr, stderr_parts), daemon=True),
    )
    for thread in threads:
        thread.start()
    status = "exited"
    try:
        while process.poll() is None:
            if overflow.is_set():
                _kill_group(process)
                status = "output_limited"
                break
            remaining = timeout_s - (time.monotonic() - started)
            if remaining <= 0:
                _kill_group(process)
                status = "timed_out"
                break
            try:
                process.wait(timeout=min(0.05, remaining))
            except subprocess.TimeoutExpired:
                continue
    finally:
        if status != "exited":
            _kill_group(process)
        for thread in threads:
            thread.join(timeout=1)
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        try:
            exit_code = process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            exit_code = None
            if status == "exited":
                status = "timed_out"
    if overflow.is_set() and status == "exited":
        status = "output_limited"
        _kill_group(process)
    stdout = b"".join(stdout_parts)
    stderr = b"".join(stderr_parts)
    if len(stdout) + len(stderr) > output_bytes_max:
        status = "output_limited"
        stdout = stdout[:output_bytes_max]
        stderr = stderr[: max(0, output_bytes_max - len(stdout))]
    duration = _elapsed(started)
    log.debug(
        "[Tools.process] backend=local outcome=%s duration_ms=%s bytes=%s",
        status,
        duration,
        len(stdout) + len(stderr),
    )
    if status in {"timed_out", "output_limited"}:
        log.warning("[Tools.process] outcome=%s", status)
    return ProcessCapture(status, exit_code if status == "exited" else exit_code, stdout, stderr, duration)


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except (ProcessLookupError, OSError):
            return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        return
