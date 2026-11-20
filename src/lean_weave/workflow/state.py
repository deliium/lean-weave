"""Contained, atomic workflow checkpoints under the configured state directory.

The public ``StateStore`` protocol stays string-in/state-out. Durable workflow
execution additionally needs one private, versioned envelope that carries
control metadata (task allowances/spend, pending effects, terminal reason).
Feature state payloads remain schema v1.0.0 and never gain chat history.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import threading
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from lean_weave.errors import StateConflict, StateCorrupt, StateError
from lean_weave.logging_setup import get_logger
from lean_weave.schemas import SCHEMA_VERSION, FeatureState
from lean_weave.workflow.results import WorkflowStatus

log = get_logger("workflow.state")

ENVELOPE_VERSION = 1
CONTROL_VERSION = 1
MAX_STATE_BYTES = 1_048_576
MAX_EFFECT_PATHS = 32
MAX_FEATURE_ID_CHARS = 128
_FEATURE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RESERVED_FEATURE_IDS = frozenset({"__llm_probe__"})
_SUPPORTED_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION})

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _thread_lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_feature_id(value: str) -> str:
    """Return a safe feature identifier or raise a stable state error."""

    if not isinstance(value, str) or not value.strip():
        raise StateError("Feature ID must be non-empty")
    if len(value) > MAX_FEATURE_ID_CHARS or _FEATURE_ID.fullmatch(value) is None:
        raise StateError("Feature ID contains unsupported characters")
    if value in _RESERVED_FEATURE_IDS:
        raise StateError("Feature ID is reserved")
    return value


def _workspace_identity(root: Path) -> str:
    return "sha256:" + hashlib.sha256(str(root).encode("utf-8")).hexdigest()


class _StateModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScopeSpend(_StateModel):
    calls: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)


class TaskSpend(ScopeSpend):
    """Per-task cumulative debit carried across attempts and resumes."""


class EffectKind(str, Enum):
    LLM_CALL = "llm_call"
    PATCH = "patch"
    CHECK = "check"


class PendingEffect(_StateModel):
    """Scoped effect identity only: no prompts, patch bodies, or raw output."""

    kind: EffectKind
    operation_id: str = Field(min_length=1, max_length=128)
    task_id: str | None = Field(default=None, max_length=MAX_FEATURE_ID_CHARS)
    revision: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    paths: list[str] = Field(default_factory=list, max_length=MAX_EFFECT_PATHS)
    calls: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    confirmed: bool = False
    created_at: str = Field(default_factory=_now, max_length=64)

    @model_validator(mode="after")
    def validate_paths(self) -> "PendingEffect":
        for path in self.paths:
            if (
                not path
                or path.startswith("/")
                or ".." in path.split("/")
                or any(ord(char) < 32 for char in path)
            ):
                raise ValueError("invalid effect path")
        return self


class ControlRecord(_StateModel):
    version: int = CONTROL_VERSION
    feature_id: str
    workspace: str
    plan_spend: ScopeSpend = Field(default_factory=ScopeSpend)
    task_spend: dict[str, TaskSpend] = Field(default_factory=dict)
    pending_effect: PendingEffect | None = None
    terminal_status: WorkflowStatus | None = None
    terminal_reason: str | None = Field(default=None, max_length=64)


class CheckpointEnvelope(_StateModel):
    envelope_version: int = ENVELOPE_VERSION
    feature_id: str
    workspace: str
    state: FeatureState
    control: ControlRecord

    @model_validator(mode="after")
    def validate_identity(self) -> "CheckpointEnvelope":
        if self.state.feature_id != self.feature_id or self.control.feature_id != self.feature_id:
            raise ValueError("checkpoint identity mismatch")
        if self.state.version not in _SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError("unsupported state schema version")
        return self


class JsonCheckpointStore:
    """Single-writer, version-checked JSON checkpoints for one workspace root."""

    def __init__(
        self,
        root: Path,
        *,
        state_dir: str = ".lean-weave",
        max_bytes: int = MAX_STATE_BYTES,
    ) -> None:
        try:
            self.root = Path(root).resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            raise StateError("Cannot resolve workflow root") from None
        relative = Path(state_dir)
        if (
            not state_dir.strip()
            or relative.is_absolute()
            or ".." in relative.parts
        ):
            raise StateError("State directory must be a contained relative path")
        self.state_dir = state_dir
        self._base = self.root / relative
        try:
            if not self._base.resolve(strict=False).is_relative_to(self.root):
                raise StateError("State directory escapes the workflow root")
        except (OSError, RuntimeError, ValueError):
            raise StateError("Cannot resolve state directory") from None
        if max_bytes <= 0:
            raise StateError("State size bound must be positive")
        self._max_bytes = max_bytes
        self._workflow = self._base / "workflow"
        self.identity = _workspace_identity(self.root)
        log.debug(
            "[State.store] root=%s state_dir=%s max_bytes=%s",
            "workspace",
            state_dir,
            max_bytes,
        )

    # ------------------------------------------------------------------
    # Layout and lease
    # ------------------------------------------------------------------
    def _ensure_layout(self) -> None:
        for directory in (self._base, self._workflow):
            if directory.is_symlink():
                raise StateCorrupt("State directory must not be a symlink")
            if directory.exists() and not directory.is_dir():
                raise StateCorrupt("State path is not a directory")
        try:
            self._workflow.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise StateError("Cannot create state directory") from None
        if self._workflow.is_symlink():
            raise StateCorrupt("State directory must not be a symlink")

    def _paths(self, feature_id: str) -> tuple[Path, Path]:
        safe = normalize_feature_id(feature_id)
        return self._workflow / f"{safe}.json", self._workflow / f"{safe}.lock"

    @contextlib.contextmanager
    def _lease(self, feature_id: str) -> Iterator[None]:
        _path, lock_path = self._paths(feature_id)
        with _thread_lock(str(lock_path)):
            self._ensure_layout()
            if lock_path.is_symlink():
                raise StateCorrupt("State lease must not be a symlink")
            try:
                fd = os.open(
                    lock_path,
                    os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                )
            except OSError:
                raise StateError("Cannot open state lease") from None
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise StateCorrupt("State lease must be a regular file")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    log.warning("[State.lease] feature=%s outcome=busy", feature_id)
                    raise StateConflict("State lease is held by another writer") from None
                except OSError:
                    raise StateError("State leasing is unavailable") from None
                try:
                    yield
                finally:
                    with contextlib.suppress(OSError):
                        fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    # ------------------------------------------------------------------
    # Decoding
    # ------------------------------------------------------------------
    def _read(self, feature_id: str) -> CheckpointEnvelope:
        state_path, _lock_path = self._paths(feature_id)
        if state_path.is_symlink():
            raise StateCorrupt("Checkpoint must not be a symlink")
        if not state_path.exists():
            raise StateError("Checkpoint is missing")
        try:
            fd = os.open(state_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            raise StateError("Cannot open checkpoint") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise StateCorrupt("Checkpoint must be a regular file")
            if info.st_size > self._max_bytes:
                raise StateCorrupt("Checkpoint exceeds the size bound")
            data = os.read(fd, self._max_bytes + 1)
        except OSError:
            raise StateError("Cannot read checkpoint") from None
        finally:
            os.close(fd)
        if len(data) > self._max_bytes:
            raise StateCorrupt("Checkpoint exceeds the size bound")
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise StateCorrupt("Checkpoint is not valid JSON") from None
        if not isinstance(payload, dict):
            raise StateCorrupt("Checkpoint root must be an object")
        return self._decode(feature_id, payload)

    def _decode(self, feature_id: str, payload: dict) -> CheckpointEnvelope:
        try:
            if "envelope_version" in payload:
                envelope = CheckpointEnvelope.model_validate(payload)
            elif "feature_id" in payload and "stage" in payload:
                # Schema v1.0.0 payload without a control envelope.
                state = FeatureState.model_validate(payload)
                envelope = CheckpointEnvelope(
                    feature_id=state.feature_id,
                    workspace=self.identity,
                    state=state,
                    control=ControlRecord(feature_id=state.feature_id, workspace=self.identity),
                )
            else:
                raise StateCorrupt("Checkpoint has no recognized shape")
        except ValidationError:
            raise StateCorrupt("Checkpoint failed schema validation") from None
        if envelope.feature_id != feature_id:
            raise StateCorrupt("Checkpoint identity does not match its path")
        if envelope.envelope_version != ENVELOPE_VERSION:
            raise StateCorrupt("Unsupported checkpoint envelope version")
        if envelope.control.version != CONTROL_VERSION:
            raise StateCorrupt("Unsupported checkpoint control version")
        if envelope.workspace != self.identity:
            raise StateCorrupt("Checkpoint belongs to a different workspace")
        return envelope

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def _encode(self, envelope: CheckpointEnvelope) -> bytes:
        try:
            text = json.dumps(
                envelope.model_dump(mode="json"),
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            raise StateError("Cannot encode checkpoint") from None
        data = text.encode("utf-8")
        if len(data) > self._max_bytes:
            raise StateCorrupt("Checkpoint exceeds the size bound")
        return data

    def _write(self, feature_id: str, envelope: CheckpointEnvelope) -> None:
        state_path, _lock_path = self._paths(feature_id)
        payload = self._encode(envelope)
        temp_name = f".lw-state-{os.getpid()}-{uuid.uuid4().hex}.tmp"
        temp_path = self._workflow / temp_name
        try:
            fd = os.open(
                temp_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
        except OSError:
            raise StateError("Cannot create checkpoint temp file") from None
        try:
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("short write")
                    view = view[written:]
                os.fsync(fd)
            except OSError:
                raise StateError("Cannot write checkpoint") from None
        finally:
            os.close(fd)
        try:
            os.replace(temp_path, state_path)
            self._sync_parent()
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(temp_path)
            raise StateError("Cannot replace checkpoint") from None
        log.debug(
            "[State.checkpoint] feature=%s stage=%s version=%s bytes=%s",
            feature_id,
            envelope.state.stage.value,
            envelope.envelope_version,
            len(payload),
        )

    def _sync_parent(self) -> None:
        try:
            fd = os.open(self._workflow, os.O_RDONLY | os.O_DIRECTORY)
        except (OSError, AttributeError):
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _write_locked(self, feature_id: str, envelope: CheckpointEnvelope) -> None:
        with self._lease(feature_id):
            self._write(feature_id, envelope)

    # ------------------------------------------------------------------
    # Public StateStore contract
    # ------------------------------------------------------------------
    def exists(self, feature_id: str) -> bool:
        state_path, _lock_path = self._paths(feature_id)
        return state_path.exists() or state_path.is_symlink()

    def load(self, feature_id: str) -> FeatureState:
        return self._read(feature_id).state

    def save(self, feature_state: FeatureState) -> None:
        feature_id = normalize_feature_id(feature_state.feature_id)
        with self._lease(feature_id):
            if self.exists(feature_id):
                control = self._read(feature_id).control
            else:
                control = ControlRecord(feature_id=feature_id, workspace=self.identity)
            envelope = CheckpointEnvelope(
                feature_id=feature_id,
                workspace=self.identity,
                state=feature_state,
                control=control,
            )
            self._write(feature_id, envelope)
        log.debug("[State.save] feature=%s stage=%s", feature_id, feature_state.stage.value)

    # ------------------------------------------------------------------
    # Private checkpoint capability
    # ------------------------------------------------------------------
    def load_checkpoint(self, feature_id: str) -> CheckpointEnvelope:
        return self._read(feature_id)

    def create_checkpoint(self, state: FeatureState, control: ControlRecord) -> CheckpointEnvelope:
        feature_id = normalize_feature_id(state.feature_id)
        if control.feature_id != feature_id:
            raise StateConflict("Control record identity does not match the feature")
        with self._lease(feature_id):
            if self.exists(feature_id):
                log.warning("[State.start] feature=%s outcome=duplicate", feature_id)
                raise StateConflict("Feature already has durable state")
            envelope = CheckpointEnvelope(
                feature_id=feature_id,
                workspace=self.identity,
                state=state,
                control=control,
            )
            self._write(feature_id, envelope)
        log.info("[State.start] feature=%s stage=%s", feature_id, state.stage.value)
        return envelope

    def save_checkpoint(self, state: FeatureState, control: ControlRecord) -> CheckpointEnvelope:
        feature_id = normalize_feature_id(state.feature_id)
        if control.feature_id != feature_id:
            raise StateConflict("Control record identity does not match the feature")
        with self._lease(feature_id):
            current = self._read(feature_id)
            if current.feature_id != feature_id:
                raise StateConflict("Checkpoint identity changed")
            envelope = CheckpointEnvelope(
                feature_id=feature_id,
                workspace=self.identity,
                state=state,
                control=control,
            )
            self._write(feature_id, envelope)
        return envelope

    def mark_effect(self, feature_id: str, effect: PendingEffect) -> CheckpointEnvelope:
        feature_id = normalize_feature_id(feature_id)
        with self._lease(feature_id):
            envelope = self._read(feature_id)
            if envelope.control.pending_effect is not None:
                raise StateConflict("An effect is already pending for this feature")
            envelope.control.pending_effect = effect
            self._write(feature_id, envelope)
        log.debug(
            "[State.effect] feature=%s kind=%s operation=%s outcome=pending",
            feature_id,
            effect.kind.value,
            effect.operation_id,
        )
        return envelope

    def clear_effect(self, feature_id: str, operation_id: str) -> CheckpointEnvelope:
        feature_id = normalize_feature_id(feature_id)
        with self._lease(feature_id):
            envelope = self._read(feature_id)
            pending = envelope.control.pending_effect
            if pending is not None:
                if pending.operation_id != operation_id:
                    raise StateConflict("Pending effect identity does not match")
                envelope.control.pending_effect = None
                self._write(feature_id, envelope)
        log.debug("[State.effect] feature=%s operation=%s outcome=cleared", feature_id, operation_id)
        return envelope

    def set_terminal(
        self, feature_id: str, status: WorkflowStatus, reason: str
    ) -> CheckpointEnvelope:
        feature_id = normalize_feature_id(feature_id)
        if len(reason) > 64 or any(ord(char) < 32 for char in reason):
            raise StateError("Terminal reason must be a bounded code")
        with self._lease(feature_id):
            envelope = self._read(feature_id)
            envelope.control.terminal_status = status
            envelope.control.terminal_reason = reason
            self._write(feature_id, envelope)
        log.info("[State.terminal] feature=%s status=%s reason=%s", feature_id, status.value, reason)
        return envelope


def checkpoint_capability(store: object) -> bool:
    """Return True when an injected store supports atomic checkpoints."""

    required = (
        "load_checkpoint",
        "create_checkpoint",
        "save_checkpoint",
        "mark_effect",
        "clear_effect",
        "set_terminal",
    )
    return all(callable(getattr(store, name, None)) for name in required)
