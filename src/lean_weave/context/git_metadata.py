"""Optional Git path/change summaries with an isolated, bounded subprocess.

Git configuration is treated as untrusted. The child environment ignores global
and system configuration, and repository configuration that could invoke
external clean/process/diff helpers disables the optional signal entirely.
No diff bodies, commit messages, or historical file contents are read.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from lean_weave.context.repository import Repository
from lean_weave.logging_setup import get_logger

log = get_logger("context.git")

GIT_TIMEOUT_S = 5.0
GIT_OUTPUT_LIMIT = 4 * 1024 * 1024
_UNSAFE_CONFIG = re.compile(
    r"^(?:filter\..+\.(?:clean|smudge|process)|diff\..+\.(?:command|textconv)|core\.fsmonitor)$",
    re.IGNORECASE,
)


@dataclass
class GitMetadata:
    available: bool = False
    reason: str | None = None
    root: str | None = None
    status: dict[str, str] = field(default_factory=dict)
    changed: dict[str, str] = field(default_factory=dict)
    recent: dict[str, int] = field(default_factory=dict)

    def recency(self, path: str) -> int | None:
        return self.recent.get(path)


def _environment() -> dict[str, str]:
    # Deliberately minimal: no inherited GIT_* overrides or global/system config.
    return {
        "PATH": os.environ.get("PATH", ""),
        "LC_ALL": "C",
        "LANG": "C",
        "HOME": os.devnull,
        "XDG_CONFIG_HOME": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_ASKPASS": "true",
        "GIT_PAGER": "cat",
    }


def _run_git(root: Path, args: list[str]) -> tuple[int, bytes] | None:
    if not args or any("\x00" in item for item in args):
        return None
    argv = [
        "git",
        "-C",
        str(root),
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "core.preloadindex=false",
        "-c",
        "core.hooksPath=",
        *args,
    ]
    try:
        completed = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_S,
            check=False,
            shell=False,
            env=_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if len(completed.stdout) > GIT_OUTPUT_LIMIT:
        return None
    return completed.returncode, completed.stdout


def _config_is_safe(root: Path) -> bool:
    result = _run_git(
        root,
        [
            "config",
            "--get-regexp",
            r"^(filter\..*\.(clean|smudge|process)|diff\..*\.(command|textconv)|core\.fsmonitor)$",
        ],
    )
    if result is None:
        return False
    code, output = result
    if code == 1 and not output.strip():
        return True
    if code != 0:
        return False
    text = output.decode("utf-8", "replace")
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 1)
        key = parts[0]
        value = parts[1].strip().lower() if len(parts) > 1 else ""
        # Our own child override disables the filesystem monitor; an explicit
        # repository value of false is equally safe.
        if key.lower() == "core.fsmonitor" and value in {"", "false", "0", "off"}:
            continue
        if _UNSAFE_CONFIG.match(key):
            return False
    return True


def _parse_status(data: bytes) -> dict[str, str]:
    tokens = data.split(b"\x00")
    result: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        try:
            text = token.decode("utf-8", "surrogateescape")
        except UnicodeDecodeError:
            continue
        if len(text) < 4:
            continue
        status = text[:2]
        path = text[3:]
        if status[0] in {"R", "C"} and index < len(tokens):
            # The current token already carries the rename destination; the
            # following NUL token is the original path (which we discard).
            index += 1
        if path:
            result[path.replace("\\", "/")] = status
    return result


def _parse_name_status(data: bytes) -> dict[str, str]:
    tokens = data.split(b"\x00")
    result: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        status = token.decode("utf-8", "surrogateescape")
        if not status:
            continue
        if status[0] in {"R", "C"}:
            if index + 1 >= len(tokens):
                break
            old = tokens[index].decode("utf-8", "surrogateescape")
            destination = tokens[index + 1].decode("utf-8", "surrogateescape")
            index += 2
            if destination:
                result[destination.replace("\\", "/")] = status
            _ = old
            continue
        if index >= len(tokens):
            break
        path = tokens[index].decode("utf-8", "surrogateescape")
        index += 1
        if path:
            result[path.replace("\\", "/")] = status
    return result


def _eligible_intersection(mapping: dict[str, str], eligible: set[str]) -> dict[str, str]:
    return {path: value for path, value in mapping.items() if path in eligible}


def read_git_metadata(repository: Repository, *, recent_commits: int | None = None) -> GitMetadata:
    root = repository.root
    eligible = set(repository.inventory().files)
    limit = recent_commits if recent_commits is not None else repository.config.recent_commits_max
    if limit <= 0:
        limit = 0
    if not (root / ".git").exists():
        log.debug("[Context.git] status=absent")
        return GitMetadata(available=False, reason="not_a_repository")
    head = _run_git(root, ["rev-parse", "--show-toplevel"])
    if head is None or head[0] != 0:
        return GitMetadata(available=False, reason="git_unusable")
    try:
        top = Path(head[1].decode("utf-8", "surrogateescape").strip()).resolve()
    except (OSError, ValueError):
        return GitMetadata(available=False, reason="git_unusable")
    if top != root:
        return GitMetadata(available=False, reason="not_project_root")
    if not _config_is_safe(root):
        log.warning("[Context.git] optional recency skipped: unsafe repository configuration")
        return GitMetadata(available=False, reason="unsafe_config")

    status = _parse_status(
        (_run_git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"]) or (1, b""))[1]
    )
    staged = _parse_name_status(
        (
            _run_git(
                root,
                ["diff", "--cached", "--name-status", "-z", "--no-ext-diff", "--no-textconv"],
            )
            or (1, b"")
        )[1]
    )
    unstaged = _parse_name_status(
        (
            _run_git(root, ["diff", "--name-status", "-z", "--no-ext-diff", "--no-textconv"])
            or (1, b"")
        )[1]
    )
    changed = {**staged, **unstaged}
    changed.update({path: value for path, value in status.items() if value.startswith("??")})

    recent: dict[str, int] = {}
    if limit:
        commits_result = _run_git(
            root, ["log", "--no-show-signature", "--format=%H", "-n", str(limit)]
        )
        if commits_result is not None and commits_result[0] == 0:
            commits = [
                line.strip()
                for line in commits_result[1].decode("ascii", "replace").splitlines()
                if line.strip()
            ]
            for position, commit in enumerate(commits):
                if not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit):
                    continue
                diff = _run_git(
                    root,
                    [
                        "diff-tree",
                        "--root",
                        "--no-commit-id",
                        "--name-status",
                        "-r",
                        "-z",
                        "--no-ext-diff",
                        "--no-textconv",
                        commit,
                    ],
                )
                if diff is None or diff[0] != 0:
                    continue
                for path, value in _parse_name_status(diff[1]).items():
                    recent.setdefault(path, position)
        elif commits_result is not None and commits_result[0] != 0:
            log.debug("[Context.git] recent history unavailable (unborn or unreadable)")

    # Working-tree changes are more recent than any committed position.
    for path in changed:
        recent[path] = -1

    metadata = GitMetadata(
        available=True,
        root=str(root),
        status=_eligible_intersection(status, eligible),
        changed=_eligible_intersection(changed, eligible),
        recent=_eligible_intersection(recent, eligible),  # type: ignore[arg-type]
    )
    log.debug(
        "[Context.git] status=ok status=%s changed=%s recent=%s",
        len(metadata.status),
        len(metadata.changed),
        len(metadata.recent),
    )
    return metadata


def recency_key(metadata: GitMetadata | None, path: str) -> int:
    """Smaller is more recent; unrelated paths receive the neutral maximum."""

    if metadata is None or not metadata.available:
        return 1 << 30
    value = metadata.recent.get(path)
    return value if value is not None else 1 << 30
