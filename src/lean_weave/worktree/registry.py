"""Durable routing records and leases for managed feature worktrees.

Routing lives under the Git common directory in a private ``lean-weave/``
tree. Records carry identity and provisioning phase only — never plans,
budgets, or checkpoint copies. OS-released exclusive leases serialize
allocation and mutating lifecycle operations across cooperating processes.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import stat
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lean_weave.errors import (
    WorktreeBusy,
    WorktreeConflict,
    WorktreeError,
    WorktreeStale,
)
from lean_weave.logging_setup import get_logger
from lean_weave.workflow.state import normalize_feature_id

log = get_logger("worktree.registry")

ROUTING_VERSION = 1
ROUTING_DIRNAME = "lean-weave"
MAX_ROUTING_BYTES = 16_384
DEFAULT_STATE_DIR = ".lean-weave"


class Ownership(str, Enum):
    MANAGED = "managed"
    ADOPTED = "adopted"


class ProvisioningPhase(str, Enum):
    RESERVED = "reserved"
    PROVISIONING = "provisioning"
    READY = "ready"
    ARCHIVING = "archiving"
    ARCHIVED = "archived"
    TOMBSTONE = "tombstone"
    FAILED = "failed"


class _RoutingModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RoutingRecord(_RoutingModel):
    version: int = Field(default=ROUTING_VERSION, ge=1)
    feature_id: str
    ownership: Ownership
    phase: ProvisioningPhase
    source_root: str
    source_common_dir: str
    source_head: str
    worktree_root: str
    worktree_git_dir: str
    worktree_common_dir: str
    branch: str
    base_commit: str
    state_dir: str = DEFAULT_STATE_DIR
    archive_root: str | None = None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DirectoryIdentity:
    path: Path
    common_dir: Path
    git_dir: Path
    head: str
    branch: str | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def repository_key(common_dir: Path) -> str:
    digest = hashlib.sha256(str(common_dir.resolve()).encode("utf-8")).hexdigest()
    return digest[:32]


def routing_root(common_dir: Path) -> Path:
    return common_dir / ROUTING_DIRNAME


def worktree_parent(source_root: Path, common_dir: Path) -> Path:
    """Owner-only sibling parent outside the source checkout."""

    key = repository_key(common_dir)
    parent = source_root.resolve().parent / f".lean-weave-worktrees-{key}"
    return parent


def archive_parent(common_dir: Path) -> Path:
    return routing_root(common_dir) / "archive"


def feature_routing_path(common_dir: Path, feature_id: str) -> Path:
    safe = normalize_feature_id(feature_id)
    return routing_root(common_dir) / "features" / f"{safe}.json"


def feature_lock_path(common_dir: Path, feature_id: str) -> Path:
    safe = normalize_feature_id(feature_id)
    return routing_root(common_dir) / "locks" / f"{safe}.lock"


def allocation_lock_path(common_dir: Path) -> Path:
    return routing_root(common_dir) / "locks" / "allocation.lock"


def cancel_marker_path(common_dir: Path, feature_id: str) -> Path:
    safe = normalize_feature_id(feature_id)
    return routing_root(common_dir) / "cancel" / f"{safe}.json"


def ensure_routing_layout(common_dir: Path) -> Path:
    root = routing_root(common_dir)
    for directory in (
        root,
        root / "features",
        root / "locks",
        root / "cancel",
        root / "archive",
    ):
        if directory.is_symlink():
            raise WorktreeError("routing_symlink")
        try:
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        except OSError as exc:
            raise WorktreeError("routing_unwritable") from exc
        if directory.is_symlink() or not directory.is_dir():
            raise WorktreeError("routing_corrupt")
    return root


@contextlib.contextmanager
def exclusive_lease(lock_path: Path) -> Iterator[None]:
    """Nonblocking exclusive lease released by the OS on process exit."""

    if lock_path.is_symlink():
        raise WorktreeError("lease_symlink")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(lock_path.parent, 0o700)
    except OSError as exc:
        raise WorktreeError("lease_unwritable") from exc
    try:
        fd = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except OSError as exc:
        raise WorktreeError("lease_unwritable") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise WorktreeError("lease_corrupt")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            log.warning("[Worktree.registry] outcome=busy")
            raise WorktreeBusy("lease_busy") from exc
        except OSError as exc:
            raise WorktreeError("lease_unavailable") from exc
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _encode(record: RoutingRecord) -> bytes:
    text = json.dumps(
        record.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    data = text.encode("utf-8")
    if len(data) > MAX_ROUTING_BYTES:
        raise WorktreeError("routing_too_large")
    return data


def write_routing(common_dir: Path, record: RoutingRecord) -> None:
    ensure_routing_layout(common_dir)
    path = feature_routing_path(common_dir, record.feature_id)
    payload = _encode(record)
    temp = path.parent / f".lw-route-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except OSError as exc:
        raise WorktreeError("routing_write_failed") from exc
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(fd)
    except OSError as exc:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise WorktreeError("routing_write_failed") from exc
    os.close(fd)
    try:
        os.replace(temp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise WorktreeError("routing_write_failed") from exc
    log.debug(
        "[Worktree.registry] op=write feature=%s phase=%s",
        record.feature_id,
        record.phase.value,
    )


def read_routing(common_dir: Path, feature_id: str) -> RoutingRecord:
    path = feature_routing_path(common_dir, feature_id)
    if path.is_symlink():
        raise WorktreeError("routing_symlink")
    if not path.exists():
        raise WorktreeError("routing_missing")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise WorktreeError("routing_unreadable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise WorktreeError("routing_corrupt")
        if info.st_size > MAX_ROUTING_BYTES:
            raise WorktreeError("routing_too_large")
        data = os.read(fd, MAX_ROUTING_BYTES + 1)
    except OSError as exc:
        raise WorktreeError("routing_unreadable") from exc
    finally:
        os.close(fd)
    if len(data) > MAX_ROUTING_BYTES:
        raise WorktreeError("routing_too_large")
    try:
        payload: Any = json.loads(data.decode("utf-8"))
        record = RoutingRecord.model_validate(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
        raise WorktreeError("routing_corrupt") from exc
    if record.feature_id != normalize_feature_id(feature_id):
        raise WorktreeError("routing_corrupt")
    if record.version != ROUTING_VERSION:
        raise WorktreeError("routing_unsupported")
    return record


def list_routing_ids(common_dir: Path) -> tuple[str, ...]:
    features = routing_root(common_dir) / "features"
    if not features.exists():
        return ()
    ids: list[str] = []
    try:
        for entry in sorted(features.iterdir()):
            if entry.is_symlink() or not entry.is_file() or not entry.name.endswith(".json"):
                continue
            ids.append(entry.name[: -len(".json")])
    except OSError as exc:
        raise WorktreeError("routing_unreadable") from exc
    return tuple(ids)


def publish_cancel_marker(common_dir: Path, feature_id: str, *, worktree_root: str) -> None:
    ensure_routing_layout(common_dir)
    path = cancel_marker_path(common_dir, feature_id)
    payload = json.dumps(
        {
            "version": 1,
            "feature_id": normalize_feature_id(feature_id),
            "worktree_root": worktree_root,
            "requested_at": _now(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    temp = path.parent / f".lw-cancel-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise WorktreeError("cancel_marker_failed") from exc
    log.info("[Worktree.registry] op=cancel_request feature=%s", feature_id)


def read_cancel_marker(common_dir: Path, feature_id: str) -> dict[str, str] | None:
    path = cancel_marker_path(common_dir, feature_id)
    if not path.exists():
        return None
    if path.is_symlink():
        raise WorktreeError("cancel_marker_corrupt")
    try:
        data = path.read_bytes()
        payload = json.loads(data.decode("utf-8"))
        if not isinstance(payload, dict):
            raise WorktreeError("cancel_marker_corrupt")
        return {str(key): str(value) for key, value in payload.items()}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise WorktreeError("cancel_marker_corrupt") from exc


def clear_cancel_marker(common_dir: Path, feature_id: str) -> None:
    path = cancel_marker_path(common_dir, feature_id)
    with contextlib.suppress(FileNotFoundError):
        if path.is_symlink():
            raise WorktreeError("cancel_marker_corrupt")
        path.unlink()


def new_feature_id() -> str:
    return uuid.uuid4().hex


def assert_identity_match(
    record: RoutingRecord,
    *,
    live_root: Path,
    live_common: Path,
    live_git_dir: Path,
    live_branch: str | None,
    live_head: str | None = None,
) -> None:
    try:
        if live_root.resolve() != Path(record.worktree_root).resolve():
            raise WorktreeStale("worktree_root_mismatch")
        if live_common.resolve() != Path(record.worktree_common_dir).resolve():
            raise WorktreeStale("common_dir_mismatch")
        if live_git_dir.resolve() != Path(record.worktree_git_dir).resolve():
            raise WorktreeStale("git_dir_mismatch")
    except (OSError, RuntimeError) as exc:
        raise WorktreeStale("identity_unresolvable") from exc
    if record.ownership == Ownership.MANAGED and live_branch != record.branch:
        raise WorktreeStale("branch_mismatch")
    if live_head is not None and live_head != record.base_commit and record.phase in {
        ProvisioningPhase.RESERVED,
        ProvisioningPhase.PROVISIONING,
    }:
        # During early phases the pin must still match; later phases may advance HEAD.
        raise WorktreeStale("head_mismatch")
    log.debug("[Worktree.registry] op=identity_ok feature=%s", record.feature_id)


def touch_phase(record: RoutingRecord, phase: ProvisioningPhase) -> RoutingRecord:
    return record.model_copy(update={"phase": phase, "updated_at": _now()})


def build_record(
    *,
    feature_id: str,
    ownership: Ownership,
    phase: ProvisioningPhase,
    source_root: Path,
    source_common_dir: Path,
    source_head: str,
    worktree_root: Path,
    worktree_git_dir: Path,
    worktree_common_dir: Path,
    branch: str,
    base_commit: str,
    state_dir: str = DEFAULT_STATE_DIR,
    archive_root: Path | None = None,
) -> RoutingRecord:
    now = _now()
    return RoutingRecord(
        feature_id=normalize_feature_id(feature_id),
        ownership=ownership,
        phase=phase,
        source_root=str(source_root.resolve()),
        source_common_dir=str(source_common_dir.resolve()),
        source_head=source_head,
        worktree_root=str(worktree_root.resolve()),
        worktree_git_dir=str(worktree_git_dir.resolve()),
        worktree_common_dir=str(worktree_common_dir.resolve()),
        branch=branch,
        base_commit=base_commit,
        state_dir=state_dir,
        archive_root=str(archive_root.resolve()) if archive_root is not None else None,
        created_at=now,
        updated_at=now,
    )
