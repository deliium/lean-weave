"""Immutable approval snapshots for standalone tools.

YAML configuration is data. A snapshot authorizes a read scope immediately, and
authorizes writes or check execution only when the trusted caller opts in.
Later mutation of the source config cannot widen the snapshot. Paths are
resolved against the explicit workspace root, never the process cwd.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from pydantic import ConfigDict

from lean_weave.config.models import AppConfig, ApprovedCheck, ToolsConfig
from lean_weave.errors import ConfigError
from lean_weave.logging_setup import get_logger

log = get_logger("executor.approval")


class _SnapshotTools(ToolsConfig):
    """Frozen copy of the effective limits.

    The source ``AppConfig`` remains mutable, but reassigning a field on the
    approval snapshot cannot widen it after approval.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _SnapshotCheck(ApprovedCheck):
    """Frozen check entry copied into an approval snapshot."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


@dataclass(frozen=True)
class ToolApproval:
    """Trusted, immutable view of one workspace and its approved policy."""

    root: Path
    state_dir: str
    config_provenance: str
    protected: tuple[str, ...]
    read_allow: tuple[str, ...]
    write_allow: tuple[str, ...]
    excludes: tuple[str, ...]
    limits: ToolsConfig
    checks: tuple[ApprovedCheck, ...]
    writes_approved: bool
    checks_approved: bool


def _fail(outcome: str) -> None:
    log.debug("[Tools.approve] outcome=%s", outcome)
    raise ConfigError("Invalid tool approval")


def _root(path: Path) -> Path:
    try:
        root = path.resolve()
    except (OSError, RuntimeError, ValueError):
        _fail("invalid_root")
    try:
        info = os.lstat(root)
    except OSError:
        _fail("invalid_root")
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        _fail("invalid_root")
    return root


def _walk_unambiguous(root: Path, relative: str, *, must_exist: bool) -> str:
    """Reject a protected relative path whose components leave the root or are symlinks."""

    parts = [part for part in relative.replace("\\", "/").split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        _fail("ambiguous_path")
    current = root
    for part in parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if must_exist:
                _fail("ambiguous_path")
            return "/".join(parts)
        except OSError:
            _fail("ambiguous_path")
        if stat.S_ISLNK(info.st_mode):
            _fail("ambiguous_path")
        try:
            if not current.resolve().is_relative_to(root):
                _fail("ambiguous_path")
        except (OSError, RuntimeError, ValueError):
            _fail("ambiguous_path")
    return "/".join(parts)


def approve_tools(
    root: Path,
    config: AppConfig | None = None,
    *,
    allow_writes: bool = False,
    allow_checks: bool = False,
    config_path: Path | None = None,
) -> ToolApproval:
    """Snapshot policy for ``root``.

    ``allow_writes`` and ``allow_checks`` are the only switches that copy write
    patterns or the check registry into the snapshot. Loading YAML does not
    call this function.
    """

    pinned = _root(root)
    source = config if config is not None else AppConfig()
    state_dir = _walk_unambiguous(pinned, source.workflow.state_dir, must_exist=False)
    if not state_dir:
        _fail("ambiguous_path")
    try:
        lexical_state = (pinned / state_dir).resolve()
        if lexical_state == pinned or not lexical_state.is_relative_to(pinned):
            _fail("ambiguous_path")
    except (OSError, RuntimeError, ValueError):
        _fail("ambiguous_path")

    protected = [state_dir, f"{state_dir}/config.yaml"]
    provenance = "none"
    if config_path is not None:
        if config_path.is_absolute():
            try:
                relative_config = config_path.relative_to(pinned).as_posix()
            except ValueError:
                provenance = "external"
                relative_config = ""
        else:
            relative_config = Path(config_path).as_posix()
            provenance = "workspace"
        if relative_config:
            protected.append(_walk_unambiguous(pinned, relative_config, must_exist=False))
            provenance = "workspace"

    data = source.tools.model_dump()
    if not allow_writes:
        data["write_allow"] = []
    if not allow_checks:
        data["checks"] = []
    limits = _SnapshotTools.model_validate(data)
    checks = tuple(_SnapshotCheck.model_validate(item.model_dump()) for item in limits.checks)
    log.debug("[FIX] approval snapshot frozen outcome=ok")
    approval = ToolApproval(
        root=pinned,
        state_dir=state_dir,
        config_provenance=provenance,
        protected=tuple(dict.fromkeys(protected)),
        read_allow=tuple(limits.read_allow),
        write_allow=tuple(limits.write_allow) if allow_writes else (),
        excludes=tuple(limits.excludes),
        limits=limits,
        checks=checks if allow_checks else (),
        writes_approved=bool(allow_writes),
        checks_approved=bool(allow_checks),
    )
    log.debug(
        "[Tools.approve] outcome=ok provenance=%s writes=%s checks=%s "
        "file_bytes_max=%s scan_bytes_max=%s range_lines_max=%s search_hits_max=%s "
        "patch_bytes_max=%s patch_hunks_max=%s checks_max=%s runtime_s_max=%s "
        "capture_bytes_max=%s result_chars_max=%s",
        approval.config_provenance,
        approval.writes_approved,
        approval.checks_approved,
        limits.file_bytes_max,
        limits.scan_bytes_max,
        limits.range_lines_max,
        limits.search_hits_max,
        limits.patch_bytes_max,
        limits.patch_hunks_max,
        limits.checks_max,
        limits.runtime_s_max,
        limits.capture_bytes_max,
        limits.result_chars_max,
    )
    return approval
