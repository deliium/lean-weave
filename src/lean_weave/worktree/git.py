"""Fixed, fail-closed Git adapter for worktree lifecycle operations.

Uses an absolute Git binary, a minimal trusted environment, fixed argv, and
``run_bounded`` capture. Repository-local executable helpers, config includes,
sparse checkout, submodules, and promisor/partial layouts are refused up front.
Linked worktrees are supported here; the general executor Git tool still rejects
gitfiles.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from lean_weave.errors import GitOperationError, UnsupportedRepository
from lean_weave.logging_setup import get_logger
from lean_weave.verify.process import ProcessCapture, run_bounded

log = get_logger("worktree.git")

GIT_TIMEOUT_S = 5.0
GIT_MUTATE_TIMEOUT_S = 30.0
GIT_OUTPUT_LIMIT = 4 * 1024 * 1024

_UNSAFE_EXEC_CONFIG = re.compile(
    r"^(?:filter\..+\.(?:clean|smudge|process)|diff\..+\.(?:command|textconv)|"
    r"core\.fsmonitor|core\.hookspath)$",
    re.IGNORECASE,
)
_INCLUDE_CONFIG = re.compile(r"^(?:include\.path|includeIf\..+\.path)$", re.IGNORECASE)
_PARTIAL_CONFIG = re.compile(
    r"^(?:extensions\.partialClone|remote\..+\.promisor|remote\..+\.partialclonefilter)$",
    re.IGNORECASE,
)
_SUBMODULE_CONFIG = re.compile(r"^submodule\..+\.path$", re.IGNORECASE)
_SHA = re.compile(r"^[0-9a-f]{40}$")
_BRANCH_REF = re.compile(r"^refs/heads/.+")


@dataclass(frozen=True)
class GitIdentity:
    """Canonical local repository identity for a checkout or linked worktree."""

    root: Path
    git_dir: Path
    common_dir: Path
    head: str
    branch: str | None


@dataclass(frozen=True)
class WorktreeEntry:
    """One entry from ``git worktree list --porcelain -z``."""

    path: Path
    head: str | None
    branch: str | None
    bare: bool = False
    detached: bool = False
    locked: bool = False
    prunable: bool = False


@dataclass(frozen=True)
class GitStatusEntry:
    """One porcelain status path. ``xy`` is the two-character index/worktree code."""

    path: str
    xy: str


@dataclass(frozen=True)
class GitStatusResult:
    """Complete local cleanliness observation. Truncation is never returned."""

    entries: tuple[GitStatusEntry, ...]


def resolve_git_executable() -> Path:
    """Return an absolute, executable Git binary from the trusted path."""

    search_path = "/usr/bin:/bin"
    located = shutil.which("git", path=search_path)
    if located is None:
        log.warning("[Worktree.git] refuse=git_unavailable")
        raise GitOperationError("git_unavailable")
    path = Path(located)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        log.warning("[Worktree.git] refuse=git_unavailable")
        raise GitOperationError("git_unavailable") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        log.warning("[Worktree.git] refuse=git_unavailable")
        raise GitOperationError("git_unavailable")
    log.debug("[Worktree.git] op=resolve_git count=1")
    return resolved


def preflight_repository(root: Path) -> GitIdentity:
    """Validate a local checkout and return its identity.

    Fail closed for missing HEAD, unsafe executable config, config includes,
    sparse-checkout, submodules, and promisor/partial clone layouts.
    """

    cwd = _resolve_existing(root)
    git = resolve_git_executable()
    _refuse_if_truncated(_git(git, cwd, ["rev-parse", "--is-inside-work-tree"]), "preflight")
    inside = _require_text(_git(git, cwd, ["rev-parse", "--is-inside-work-tree"]), "not_a_repository")
    if inside.strip() != "true":
        log.warning("[Worktree.git] refuse=not_a_repository")
        raise UnsupportedRepository("not_a_repository")

    toplevel = _require_path(_git(git, cwd, ["rev-parse", "--show-toplevel"]), "git_unusable")
    git_dir = _require_path(_git(git, cwd, ["rev-parse", "--absolute-git-dir"]), "git_unusable")
    common_raw = _require_text(_git(git, cwd, ["rev-parse", "--git-common-dir"]), "git_unusable")
    common_dir = _resolve_git_path(toplevel, common_raw)

    head_capture = _git(git, cwd, ["rev-parse", "--verify", "HEAD"])
    _refuse_if_truncated(head_capture, "preflight")
    if head_capture.status != "exited" or head_capture.exit_code != 0:
        log.warning("[Worktree.git] refuse=missing_head")
        raise UnsupportedRepository("missing_head")
    head = head_capture.stdout.decode("ascii", "replace").strip()
    if not _SHA.fullmatch(head):
        log.warning("[Worktree.git] refuse=missing_head")
        raise UnsupportedRepository("missing_head")

    object_probe = _git(git, cwd, ["cat-file", "-e", f"{head}^{{commit}}"])
    _refuse_if_truncated(object_probe, "preflight")
    if object_probe.status != "exited" or object_probe.exit_code != 0:
        log.warning("[Worktree.git] refuse=missing_objects")
        raise UnsupportedRepository("missing_objects")

    _check_executable_config(git, cwd)
    _check_include_config(git, cwd)
    _check_partial_config(git, cwd)
    _check_sparse_checkout(git, cwd, common_dir, git_dir)
    _check_submodules(git, cwd, toplevel, common_dir)
    _check_hook_executables(git, cwd, git_dir, common_dir)

    branch = _current_branch(git, cwd)
    identity = GitIdentity(
        root=toplevel,
        git_dir=git_dir,
        common_dir=common_dir,
        head=head,
        branch=branch,
    )
    log.debug("[Worktree.git] op=preflight count=1")
    return identity


def list_worktrees(identity: GitIdentity) -> tuple[WorktreeEntry, ...]:
    """List registered worktrees with NUL-delimited porcelain parsing."""

    git = resolve_git_executable()
    capture = _git(
        git,
        identity.root,
        ["worktree", "list", "--porcelain", "-z"],
        timeout_s=GIT_TIMEOUT_S,
    )
    _refuse_if_truncated(capture, "list_worktrees")
    if capture.status != "exited" or capture.exit_code != 0:
        log.warning("[Worktree.git] refuse=list_failed")
        raise GitOperationError("list_failed")
    entries = _parse_worktree_list(capture.stdout)
    if entries is None:
        log.warning("[Worktree.git] refuse=list_unusable")
        raise GitOperationError("list_unusable")
    log.debug("[Worktree.git] op=list_worktrees count=%s", len(entries))
    return entries


def add_worktree(
    identity: GitIdentity,
    *,
    branch: str,
    path: Path,
    start: str,
) -> GitIdentity:
    """Create a linked worktree on a new branch starting at ``start``."""

    if not branch or "\x00" in branch or branch.startswith("-"):
        raise GitOperationError("invalid_branch")
    if not start or "\x00" in start or start.startswith("-"):
        raise GitOperationError("invalid_start")
    target = path if path.is_absolute() else identity.root / path
    try:
        target = target.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise GitOperationError("invalid_path") from exc
    if target.exists():
        log.warning("[Worktree.git] refuse=path_exists")
        raise GitOperationError("path_exists")

    git = resolve_git_executable()
    capture = _git(
        git,
        identity.root,
        ["worktree", "add", "-b", branch, str(target), start],
        timeout_s=GIT_MUTATE_TIMEOUT_S,
    )
    _refuse_if_truncated(capture, "add_worktree")
    if capture.status != "exited" or capture.exit_code != 0:
        log.warning("[Worktree.git] refuse=add_failed")
        raise GitOperationError("add_failed")
    created = preflight_repository(target)
    log.debug("[Worktree.git] op=add_worktree count=1")
    return created


def remove_worktree(identity: GitIdentity, path: Path) -> None:
    """Remove a linked worktree without ``--force``."""

    target = path if path.is_absolute() else identity.root / path
    try:
        target = target.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise GitOperationError("invalid_path") from exc

    git = resolve_git_executable()
    capture = _git(
        git,
        identity.root,
        ["worktree", "remove", str(target)],
        timeout_s=GIT_MUTATE_TIMEOUT_S,
    )
    _refuse_if_truncated(capture, "remove_worktree")
    if capture.status != "exited" or capture.exit_code != 0:
        log.warning("[Worktree.git] refuse=remove_failed")
        raise GitOperationError("remove_failed")
    log.debug("[Worktree.git] op=remove_worktree count=1")


def read_status(
    identity: GitIdentity,
    *,
    include_ignored: bool = False,
) -> GitStatusResult:
    """Read porcelain status. Truncated or timed-out output fails closed."""

    git = resolve_git_executable()
    args = ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"]
    if include_ignored:
        args.append("--ignored=matching")
    capture = _git(git, identity.root, args, timeout_s=GIT_TIMEOUT_S)
    if capture.status in {"output_limited", "timed_out"}:
        log.warning("[Worktree.git] refuse=status_truncated")
        raise GitOperationError("status_truncated")
    if capture.status != "exited" or capture.exit_code not in {0, 1}:
        log.warning("[Worktree.git] refuse=status_failed")
        raise GitOperationError("status_failed")
    entries = _parse_status(capture.stdout)
    if entries is None:
        log.warning("[Worktree.git] refuse=status_unusable")
        raise GitOperationError("status_unusable")
    log.debug("[Worktree.git] op=read_status count=%s", len(entries))
    return GitStatusResult(entries=entries)


def _trusted_env() -> dict[str, str]:
    """Minimal environment with no inherited ``GIT_*`` overrides."""

    return {
        "PATH": "/usr/bin:/bin",
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


def _git_prefix(git: Path, cwd: Path) -> list[str]:
    return [
        str(git),
        "-C",
        str(cwd),
        "-c",
        "core.hooksPath=",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "core.preloadindex=false",
    ]


def _git(
    git: Path,
    cwd: Path,
    args: list[str],
    *,
    timeout_s: float = GIT_TIMEOUT_S,
    output_bytes_max: int = GIT_OUTPUT_LIMIT,
) -> ProcessCapture:
    if not args or any("\x00" in item for item in args):
        return ProcessCapture("spawn_error", None, b"", b"", 0)
    return run_bounded(
        [*_git_prefix(git, cwd), *args],
        cwd=cwd,
        env=_trusted_env(),
        timeout_s=timeout_s,
        output_bytes_max=output_bytes_max,
    )


def _resolve_existing(root: Path) -> Path:
    try:
        return root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        log.warning("[Worktree.git] refuse=path_unusable")
        raise UnsupportedRepository("path_unusable") from exc


def _resolve_git_path(toplevel: Path, raw: str) -> Path:
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = toplevel / candidate
    try:
        return candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        log.warning("[Worktree.git] refuse=git_unusable")
        raise UnsupportedRepository("git_unusable") from exc


def _require_text(capture: ProcessCapture, code: str) -> str:
    _refuse_if_truncated(capture, "preflight")
    if capture.status != "exited" or capture.exit_code != 0:
        log.warning("[Worktree.git] refuse=%s", code)
        raise UnsupportedRepository(code)
    try:
        return capture.stdout.decode("utf-8").strip()
    except UnicodeError as exc:
        log.warning("[Worktree.git] refuse=%s", code)
        raise UnsupportedRepository(code) from exc


def _require_path(capture: ProcessCapture, code: str) -> Path:
    text = _require_text(capture, code)
    try:
        return Path(text).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        log.warning("[Worktree.git] refuse=%s", code)
        raise UnsupportedRepository(code) from exc


def _refuse_if_truncated(capture: ProcessCapture, op: str) -> None:
    if capture.status in {"output_limited", "timed_out"}:
        log.warning("[Worktree.git] refuse=%s_truncated", op)
        raise GitOperationError(f"{op}_truncated")


def _config_regexp(git: Path, cwd: Path, pattern: str) -> tuple[str, str]:
    capture = _git(git, cwd, ["config", "--get-regexp", pattern])
    _refuse_if_truncated(capture, "preflight")
    if capture.status != "exited":
        log.warning("[Worktree.git] refuse=config_unusable")
        raise UnsupportedRepository("config_unusable")
    if capture.exit_code == 1 and not capture.stdout.strip():
        return "empty", ""
    if capture.exit_code != 0:
        log.warning("[Worktree.git] refuse=config_unusable")
        raise UnsupportedRepository("config_unusable")
    try:
        return "ok", capture.stdout.decode("utf-8", "replace")
    except UnicodeError as exc:
        raise UnsupportedRepository("config_unusable") from exc


def _check_executable_config(git: Path, cwd: Path) -> None:
    status, text = _config_regexp(
        git,
        cwd,
        r"^(filter\..*\.(clean|smudge|process)|diff\..*\.(command|textconv)|core\.fsmonitor|core\.hooksPath)$",
    )
    if status == "empty":
        return
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 1)
        key = parts[0]
        value = parts[1].strip() if len(parts) > 1 else ""
        lower_key = key.lower()
        lower_value = value.lower()
        if lower_key == "core.fsmonitor" and lower_value in {"", "false", "0", "off"}:
            continue
        if lower_key == "core.hookspath" and value == "":
            continue
        if _UNSAFE_EXEC_CONFIG.match(key):
            log.warning("[Worktree.git] refuse=unsafe_config")
            raise UnsupportedRepository("unsafe_config")


def _check_include_config(git: Path, cwd: Path) -> None:
    status, text = _config_regexp(git, cwd, r"^(include\.path|includeIf\..+\.path)$")
    if status == "empty":
        return
    for line in text.splitlines():
        if not line.strip():
            continue
        key = line.split(None, 1)[0]
        if _INCLUDE_CONFIG.match(key):
            log.warning("[Worktree.git] refuse=config_include")
            raise UnsupportedRepository("config_include")


def _check_partial_config(git: Path, cwd: Path) -> None:
    status, text = _config_regexp(
        git,
        cwd,
        r"^(extensions\.partialClone|remote\..+\.promisor|remote\..+\.partialclonefilter)$",
    )
    if status == "ok":
        for line in text.splitlines():
            if not line.strip():
                continue
            key = line.split(None, 1)[0]
            if _PARTIAL_CONFIG.match(key):
                log.warning("[Worktree.git] refuse=partial_clone")
                raise UnsupportedRepository("partial_clone")

    shallow = _git(git, cwd, ["rev-parse", "--is-shallow-repository"])
    _refuse_if_truncated(shallow, "preflight")
    if shallow.status != "exited" or shallow.exit_code != 0:
        log.warning("[Worktree.git] refuse=git_unusable")
        raise UnsupportedRepository("git_unusable")
    if shallow.stdout.decode("ascii", "replace").strip() == "true":
        log.warning("[Worktree.git] refuse=shallow_repository")
        raise UnsupportedRepository("shallow_repository")


def _check_sparse_checkout(git: Path, cwd: Path, common_dir: Path, git_dir: Path) -> None:
    status, text = _config_regexp(git, cwd, r"^core\.sparseCheckout$")
    if status == "ok":
        for line in text.splitlines():
            parts = line.split(None, 1)
            value = parts[1].strip().lower() if len(parts) > 1 else ""
            if value in {"true", "1", "yes", "on"}:
                log.warning("[Worktree.git] refuse=sparse_checkout")
                raise UnsupportedRepository("sparse_checkout")
    _ = (common_dir, git_dir)

def _check_submodules(git: Path, cwd: Path, toplevel: Path, common_dir: Path) -> None:
    gitmodules = toplevel / ".gitmodules"
    try:
        if gitmodules.is_file():
            log.warning("[Worktree.git] refuse=submodules")
            raise UnsupportedRepository("submodules")
    except OSError as exc:
        raise UnsupportedRepository("submodules") from exc

    status, text = _config_regexp(git, cwd, r"^submodule\..+\.path$")
    if status == "ok":
        for line in text.splitlines():
            if not line.strip():
                continue
            key = line.split(None, 1)[0]
            if _SUBMODULE_CONFIG.match(key):
                log.warning("[Worktree.git] refuse=submodules")
                raise UnsupportedRepository("submodules")

    # Fail closed on gitlink entries already in the index/tree.
    ls = _git(git, cwd, ["ls-files", "-z", "--stage"])
    _refuse_if_truncated(ls, "preflight")
    if ls.status != "exited" or ls.exit_code != 0:
        log.warning("[Worktree.git] refuse=submodules")
        raise UnsupportedRepository("submodules")
    for record in ls.stdout.split(b"\0"):
        if not record:
            continue
        try:
            meta = record.split(b"\t", 1)[0].decode("ascii", "replace")
        except UnicodeError:
            continue
        parts = meta.split()
        if parts and parts[0] == "160000":
            log.warning("[Worktree.git] refuse=submodules")
            raise UnsupportedRepository("submodules")
    _ = common_dir


def _check_hook_executables(
    git: Path,
    cwd: Path,
    git_dir: Path,
    common_dir: Path,
) -> None:
    status, text = _config_regexp(git, cwd, r"^core\.hooksPath$")
    hook_dirs: list[Path] = []
    if status == "ok":
        for line in text.splitlines():
            parts = line.split(None, 1)
            value = parts[1].strip() if len(parts) > 1 else ""
            if value == "":
                continue
            # Non-empty hooksPath is refused even though child argv clears it.
            log.warning("[Worktree.git] refuse=hooks_path")
            raise UnsupportedRepository("hooks_path")
    hook_dirs.append(common_dir / "hooks")
    if git_dir != common_dir:
        hook_dirs.append(git_dir / "hooks")
    for hooks in hook_dirs:
        try:
            if not hooks.is_dir():
                continue
            for entry in hooks.iterdir():
                if entry.name.endswith(".sample"):
                    continue
                try:
                    info = entry.lstat()
                except OSError:
                    continue
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    continue
                if info.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
                    log.warning("[Worktree.git] refuse=hook_executable")
                    raise UnsupportedRepository("hook_executable")
        except UnsupportedRepository:
            raise
        except OSError as exc:
            raise UnsupportedRepository("hook_executable") from exc


def _current_branch(git: Path, cwd: Path) -> str | None:
    capture = _git(git, cwd, ["symbolic-ref", "-q", "HEAD"])
    _refuse_if_truncated(capture, "preflight")
    if capture.status != "exited":
        raise UnsupportedRepository("git_unusable")
    if capture.exit_code != 0:
        return None
    ref = capture.stdout.decode("utf-8", "replace").strip()
    if not _BRANCH_REF.fullmatch(ref):
        return None
    return ref.removeprefix("refs/heads/")


def _parse_worktree_list(data: bytes) -> tuple[WorktreeEntry, ...] | None:
    if not data:
        return ()
    if data.endswith(b"\0"):
        data = data[:-1]
    blocks = data.split(b"\0\0")
    entries: list[WorktreeEntry] = []
    for block in blocks:
        if not block:
            continue
        path: Path | None = None
        head: str | None = None
        branch: str | None = None
        bare = False
        detached = False
        locked = False
        prunable = False
        for token in block.split(b"\0"):
            if not token:
                continue
            try:
                text = token.decode("utf-8")
            except UnicodeError:
                return None
            if text.startswith("worktree "):
                try:
                    path = Path(text[len("worktree ") :]).resolve()
                except (OSError, RuntimeError, ValueError):
                    return None
            elif text.startswith("HEAD "):
                value = text[len("HEAD ") :]
                if not _SHA.fullmatch(value):
                    return None
                head = value
            elif text.startswith("branch "):
                ref = text[len("branch ") :]
                if not _BRANCH_REF.fullmatch(ref):
                    return None
                branch = ref.removeprefix("refs/heads/")
            elif text == "bare":
                bare = True
            elif text == "detached":
                detached = True
            elif text.startswith("locked"):
                locked = True
            elif text.startswith("prunable"):
                prunable = True
            else:
                return None
        if path is None:
            return None
        entries.append(
            WorktreeEntry(
                path=path,
                head=head,
                branch=branch,
                bare=bare,
                detached=detached,
                locked=locked,
                prunable=prunable,
            )
        )
    return tuple(entries)


def _parse_status(data: bytes) -> tuple[GitStatusEntry, ...] | None:
    if not data:
        return ()
    tokens = data.split(b"\0")
    entries: list[GitStatusEntry] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        try:
            text = token.decode("utf-8", "surrogateescape")
        except UnicodeError:
            return None
        if len(text) < 4:
            return None
        xy = text[:2]
        path = text[3:]
        if xy[0] in {"R", "C"}:
            # --no-renames should prevent this; treat as unusable if present.
            return None
        if not path or path.startswith("/") or "\\" in path or any(ord(ch) < 32 for ch in path):
            return None
        entries.append(GitStatusEntry(path=path.replace("\\", "/"), xy=xy))
    return tuple(entries)
