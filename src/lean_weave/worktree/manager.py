"""WorktreeManager: reserve, provision, adopt, inspect, and clean up.

Preserves the origin checkout byte-for-byte. Never merges, commits, resets,
or force-removes. Concurrent mutation is serialized with registry leases.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from lean_weave.errors import (
    GitOperationError,
    LeanWeaveError,
    UnsupportedRepository,
    WorktreeBusy,
    WorktreeConflict,
    WorktreeError,
    WorktreeStale,
)
from lean_weave.logging_setup import get_logger
from lean_weave.schemas import Stage
from lean_weave.workflow.state import JsonCheckpointStore, normalize_feature_id
from lean_weave.worktree import registry
from lean_weave.worktree.git import (
    GitIdentity,
    GitStatusResult,
    _git,
    add_worktree,
    list_worktrees,
    preflight_repository,
    read_status,
    remove_worktree,
    resolve_git_executable,
)
from lean_weave.worktree.registry import (
    Ownership,
    ProvisioningPhase,
    RoutingRecord,
    assert_identity_match,
    build_record,
    clear_cancel_marker,
    exclusive_lease,
    feature_lock_path,
    new_feature_id,
    publish_cancel_marker,
    read_cancel_marker,
    read_routing,
    touch_phase,
    write_routing,
)

log = get_logger("worktree.manager")

_APP_STATE_FILE = "llm-usage.jsonl"


@dataclass(frozen=True)
class WorktreeInspection:
    feature_id: str
    ownership: Ownership
    phase: ProvisioningPhase
    source_root: Path
    worktree_root: Path
    branch: str
    base_commit: str
    state_dir: str
    cancel_requested: bool
    status: GitStatusResult | None
    live: GitIdentity | None
    reason: str | None = None


@dataclass(frozen=True)
class ProvisionedWorktree:
    feature_id: str
    record: RoutingRecord
    identity: GitIdentity


class WorktreeManager:
    """Lifecycle controller bound to an invocation checkout."""

    def __init__(self, source_cwd: Path, *, state_dir: str = registry.DEFAULT_STATE_DIR) -> None:
        self._source = preflight_repository(source_cwd)
        self._state_dir = state_dir
        relative = Path(state_dir)
        if not state_dir.strip() or relative.is_absolute() or ".." in relative.parts:
            raise WorktreeError("invalid_state_dir")
        registry.ensure_routing_layout(self._source.common_dir)
        log.debug("[Worktree.manager] op=init ownership=source")

    @property
    def source(self) -> GitIdentity:
        return self._source

    def create(self) -> ProvisionedWorktree:
        """Reserve a feature ID and provision a managed linked worktree."""

        common = self._source.common_dir
        with exclusive_lease(registry.allocation_lock_path(common)):
            feature_id = self._allocate_id()
            branch = f"feature/{feature_id}"
            parent = registry.worktree_parent(self._source.root, common)
            try:
                parent.mkdir(parents=True, exist_ok=True)
                os.chmod(parent, 0o700)
            except OSError as exc:
                raise WorktreeError("worktree_parent_unwritable") from exc
            target = parent / feature_id
            if target.exists():
                raise WorktreeConflict("worktree_path_exists")
            self._refuse_branch_collision(branch)
            self._refuse_preexisting_state(target)

            record = build_record(
                feature_id=feature_id,
                ownership=Ownership.MANAGED,
                phase=ProvisioningPhase.RESERVED,
                source_root=self._source.root,
                source_common_dir=common,
                source_head=self._source.head,
                worktree_root=target,
                worktree_git_dir=target,  # placeholder until provisioned
                worktree_common_dir=common,
                branch=branch,
                base_commit=self._source.head,
                state_dir=self._state_dir,
            )
            write_routing(common, record)
            log.info("[Worktree.manager] op=reserve feature=%s", feature_id)

            with exclusive_lease(feature_lock_path(common, feature_id)):
                write_routing(common, touch_phase(record, ProvisioningPhase.PROVISIONING))
                try:
                    created = add_worktree(
                        self._source,
                        branch=branch,
                        path=target,
                        start=self._source.head,
                    )
                except (GitOperationError, UnsupportedRepository, WorktreeError, OSError) as exc:
                    failed = touch_phase(record, ProvisioningPhase.FAILED)
                    write_routing(common, failed)
                    log.warning("[Worktree.manager] op=provision outcome=failed feature=%s", feature_id)
                    raise WorktreeError("provision_failed") from exc

                ready = build_record(
                    feature_id=feature_id,
                    ownership=Ownership.MANAGED,
                    phase=ProvisioningPhase.READY,
                    source_root=self._source.root,
                    source_common_dir=common,
                    source_head=self._source.head,
                    worktree_root=created.root,
                    worktree_git_dir=created.git_dir,
                    worktree_common_dir=created.common_dir,
                    branch=branch,
                    base_commit=self._source.head,
                    state_dir=self._state_dir,
                )
                # Preserve created_at from reservation when possible.
                ready = ready.model_copy(update={"created_at": record.created_at})
                write_routing(common, ready)
                log.info("[Worktree.manager] op=ready feature=%s", feature_id)
                return ProvisionedWorktree(feature_id=feature_id, record=ready, identity=created)

    def adopt(self, worktree_path: Path) -> ProvisionedWorktree:
        """Adopt an explicit clean linked worktree in the same repository."""

        common = self._source.common_dir
        candidate = preflight_repository(worktree_path)
        if candidate.root.resolve() == self._source.root.resolve():
            raise WorktreeConflict("primary_checkout_forbidden")
        if candidate.common_dir.resolve() != common.resolve():
            raise WorktreeConflict("foreign_repository")
        if candidate.branch is None:
            raise WorktreeError("adopt_detached")
        status = read_status(candidate, include_ignored=True)
        if status.entries:
            raise WorktreeError("adopt_dirty")
        self._refuse_preexisting_state(candidate.root)

        with exclusive_lease(registry.allocation_lock_path(common)):
            # Refuse if this path is already registered.
            for existing_id in registry.list_routing_ids(common):
                try:
                    existing = read_routing(common, existing_id)
                except WorktreeError:
                    continue
                if existing.phase == ProvisioningPhase.TOMBSTONE:
                    continue
                try:
                    if Path(existing.worktree_root).resolve() == candidate.root.resolve():
                        raise WorktreeConflict("worktree_already_registered")
                except (OSError, RuntimeError) as exc:
                    raise WorktreeStale("routing_path_unresolvable") from exc

            feature_id = self._allocate_id()
            record = build_record(
                feature_id=feature_id,
                ownership=Ownership.ADOPTED,
                phase=ProvisioningPhase.READY,
                source_root=self._source.root,
                source_common_dir=common,
                source_head=self._source.head,
                worktree_root=candidate.root,
                worktree_git_dir=candidate.git_dir,
                worktree_common_dir=candidate.common_dir,
                branch=candidate.branch,
                base_commit=candidate.head,
                state_dir=self._state_dir,
            )
            with exclusive_lease(feature_lock_path(common, feature_id)):
                write_routing(common, record)
                log.info("[Worktree.manager] op=adopt feature=%s", feature_id)
                return ProvisionedWorktree(feature_id=feature_id, record=record, identity=candidate)

    def lookup(self, feature_id: str) -> RoutingRecord:
        return read_routing(self._source.common_dir, normalize_feature_id(feature_id))

    def inspect(self, feature_id: str) -> WorktreeInspection:
        """Read-only inspection; does not take the mutating lease."""

        record = self.lookup(feature_id)
        cancel = read_cancel_marker(self._source.common_dir, record.feature_id) is not None
        if record.phase in {ProvisioningPhase.TOMBSTONE, ProvisioningPhase.ARCHIVED}:
            return WorktreeInspection(
                feature_id=record.feature_id,
                ownership=record.ownership,
                phase=record.phase,
                source_root=Path(record.source_root),
                worktree_root=Path(record.worktree_root),
                branch=record.branch,
                base_commit=record.base_commit,
                state_dir=record.state_dir,
                cancel_requested=cancel,
                status=None,
                live=None,
                reason="archived" if record.phase == ProvisioningPhase.ARCHIVED else "tombstone",
            )
        try:
            live = preflight_repository(Path(record.worktree_root))
            assert_identity_match(
                record,
                live_root=live.root,
                live_common=live.common_dir,
                live_git_dir=live.git_dir,
                live_branch=live.branch,
            )
            status = read_status(live, include_ignored=False)
            return WorktreeInspection(
                feature_id=record.feature_id,
                ownership=record.ownership,
                phase=record.phase,
                source_root=Path(record.source_root),
                worktree_root=live.root,
                branch=record.branch,
                base_commit=record.base_commit,
                state_dir=record.state_dir,
                cancel_requested=cancel,
                status=status,
                live=live,
            )
        except (UnsupportedRepository, GitOperationError, WorktreeStale, WorktreeError) as exc:
            log.warning("[Worktree.manager] op=inspect outcome=stale feature=%s", record.feature_id)
            return WorktreeInspection(
                feature_id=record.feature_id,
                ownership=record.ownership,
                phase=record.phase,
                source_root=Path(record.source_root),
                worktree_root=Path(record.worktree_root),
                branch=record.branch,
                base_commit=record.base_commit,
                state_dir=record.state_dir,
                cancel_requested=cancel,
                status=None,
                live=None,
                reason=str(exc) or "stale",
            )

    @contextlib.contextmanager
    def lease(self, feature_id: str):
        safe = normalize_feature_id(feature_id)
        with exclusive_lease(feature_lock_path(self._source.common_dir, safe)):
            yield self.lookup(safe)

    def request_cancel(self, feature_id: str) -> str:
        """Publish a durable cancel request or finalize when idle.

        Returns ``requested`` when the execution lease is busy, ``idle`` when
        the caller should finalize under the lease, or ``missing``.
        """

        safe = normalize_feature_id(feature_id)
        common = self._source.common_dir
        record = read_routing(common, safe)
        if record.phase in {
            ProvisioningPhase.TOMBSTONE,
            ProvisioningPhase.ARCHIVED,
            ProvisioningPhase.FAILED,
        }:
            return "terminal"
        lock = feature_lock_path(common, safe)
        try:
            with exclusive_lease(lock):
                return "idle"
        except WorktreeBusy:
            publish_cancel_marker(common, safe, worktree_root=record.worktree_root)
            return "requested"

    def consume_cancel_marker(self, feature_id: str) -> bool:
        marker = read_cancel_marker(self._source.common_dir, feature_id)
        if marker is None:
            return False
        clear_cancel_marker(self._source.common_dir, feature_id)
        log.info("[Worktree.manager] op=cancel_consumed feature=%s", feature_id)
        return True

    def cleanup(self, feature_id: str, *, confirm: bool) -> RoutingRecord:
        """Remove an eligible clean managed worktree after explicit confirmation."""

        if not confirm:
            raise WorktreeError("cleanup_unconfirmed")
        safe = normalize_feature_id(feature_id)
        common = self._source.common_dir
        with exclusive_lease(feature_lock_path(common, safe)):
            record = read_routing(common, safe)
            if record.ownership != Ownership.MANAGED:
                raise WorktreeError("cleanup_adopted_forbidden")
            if record.phase == ProvisioningPhase.TOMBSTONE:
                return record
            if record.phase in {
                ProvisioningPhase.RESERVED,
                ProvisioningPhase.PROVISIONING,
                ProvisioningPhase.FAILED,
            }:
                raise WorktreeError("cleanup_manual_recovery")
            if record.phase == ProvisioningPhase.ARCHIVED:
                return record

            # Terminal state must be provable before any removal or archival.
            archive_root = (
                Path(record.archive_root)
                if record.archive_root
                else registry.archive_parent(common) / safe
            )
            if record.phase == ProvisioningPhase.ARCHIVING:
                if not archive_root.exists():
                    raise WorktreeError("cleanup_manual_recovery")
                terminal = True
            else:
                terminal = self._validate_terminal_checkpoint(record)
            if read_cancel_marker(common, safe) is not None:
                if not terminal:
                    raise WorktreeError("cleanup_cancel_pending")
                clear_cancel_marker(common, safe)

            live = preflight_repository(Path(record.worktree_root))
            assert_identity_match(
                record,
                live_root=live.root,
                live_common=live.common_dir,
                live_git_dir=live.git_dir,
                live_branch=live.branch,
            )
            cleanliness = read_status(live, include_ignored=True)
            if not cleanliness.entries:
                if (live.root / record.state_dir).exists():
                    # A clean tree with app state must go through archival, never
                    # a blind remove.
                    raise WorktreeError("cleanup_state_present")
                remove_worktree(self._source, live.root)
                tombstone = touch_phase(record, ProvisioningPhase.TOMBSTONE)
                if record.phase == ProvisioningPhase.ARCHIVING:
                    tombstone = tombstone.model_copy(
                        update={"archive_root": str(archive_root.resolve())}
                    )
                write_routing(common, tombstone)
                log.info("[Worktree.manager] op=cleanup feature=%s", safe)
                return tombstone

            app_only = self._classify_app_owned_dirt(live.root, record, cleanliness)
            if app_only is None:
                raise WorktreeError("cleanup_dirty")
            write_routing(common, touch_phase(record, ProvisioningPhase.ARCHIVING))
            self._archive_state(live.root, record, archive_root, app_only)
            # Recheck cleanliness after archival.
            after = read_status(live, include_ignored=True)
            if after.entries:
                raise WorktreeError("cleanup_dirty_after_archive")
            remove_worktree(self._source, live.root)
            final = touch_phase(record, ProvisioningPhase.TOMBSTONE).model_copy(
                update={"archive_root": str(archive_root.resolve()), "phase": ProvisioningPhase.TOMBSTONE}
            )
            write_routing(common, final)
            log.info("[Worktree.manager] op=cleanup_archived feature=%s", safe)
            return final

    def _validate_terminal_checkpoint(self, record: RoutingRecord) -> bool:
        """Return True for a provable terminal run, False when never started."""

        state_root = Path(record.worktree_root) / record.state_dir
        if not state_root.exists():
            return False
        try:
            store = JsonCheckpointStore(
                Path(record.worktree_root), state_dir=record.state_dir
            )
            envelope = store.load_checkpoint(record.feature_id)
        except LeanWeaveError as exc:
            log.warning(
                "[Worktree.manager] op=cleanup feature=%s outcome=state_unavailable",
                record.feature_id,
            )
            raise WorktreeError("cleanup_state_unavailable") from exc
        if envelope.control.pending_effect is not None:
            raise WorktreeError("cleanup_pending_effect")
        if envelope.state.stage not in {Stage.DONE, Stage.FAILED}:
            raise WorktreeError("cleanup_active_run")
        return True

    def revalidate(self, feature_id: str) -> ProvisionedWorktree:
        """Re-read routing and live identity under the feature lease."""

        with self.lease(feature_id) as record:
            if record.phase != ProvisioningPhase.READY:
                raise WorktreeError("not_ready")
            live = preflight_repository(Path(record.worktree_root))
            assert_identity_match(
                record,
                live_root=live.root,
                live_common=live.common_dir,
                live_git_dir=live.git_dir,
                live_branch=live.branch,
            )
            if live.head != record.base_commit:
                raise WorktreeStale("head_mismatch")
            return ProvisionedWorktree(feature_id=record.feature_id, record=record, identity=live)

    def _allocate_id(self) -> str:
        for _ in range(32):
            feature_id = new_feature_id()
            path = registry.feature_routing_path(self._source.common_dir, feature_id)
            if not path.exists():
                normalize_feature_id(feature_id)
                return feature_id
        raise WorktreeConflict("feature_id_exhausted")

    def _refuse_branch_collision(self, branch: str) -> None:
        entries = list_worktrees(self._source)
        for entry in entries:
            if entry.branch == branch:
                raise WorktreeConflict("branch_checked_out")
        git = resolve_git_executable()
        capture = _git(
            git,
            self._source.root,
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        )
        if capture.status == "exited" and capture.exit_code == 0:
            raise WorktreeConflict("branch_exists")

    def _refuse_preexisting_state(self, worktree_root: Path) -> None:
        state = worktree_root / self._state_dir
        if state.exists():
            raise WorktreeConflict("state_path_exists")

    def _classify_app_owned_dirt(
        self,
        worktree_root: Path,
        record: RoutingRecord,
        status: GitStatusResult,
    ) -> tuple[Path, ...] | None:
        """Return app-owned relative paths when every dirty path is recognized."""

        state_dir = record.state_dir.rstrip("/")
        state_prefix = state_dir + "/"
        feature_files = {
            f"{state_dir}/workflow/{record.feature_id}.json",
            f"{state_dir}/workflow/{record.feature_id}.lock",
            f"{state_dir}/metrics/{_APP_STATE_FILE}",
            f"{state_dir}/metrics/{_APP_STATE_FILE}.lock",
        }
        owned: list[Path] = []
        for entry in status.entries:
            path = entry.path.rstrip("/")
            if path == state_dir:
                owned.append(Path(state_dir))
                continue
            if path == f"{state_dir}/{_APP_STATE_FILE}" or path in feature_files:
                owned.append(Path(path))
                continue
            if path.startswith(state_prefix):
                # Directory entries are revalidated exhaustively below; a file
                # the workflow does not own blocks cleanup.
                if not (worktree_root / path).is_dir():
                    return None
                owned.append(Path(path))
                continue
            return None

        state_root = worktree_root / state_dir
        if not state_root.exists():
            return tuple(owned)
        try:
            for current, dirnames, filenames in os.walk(state_root, followlinks=False):
                current_path = Path(current)
                if current_path.is_symlink():
                    return None
                relative_parent = current_path.relative_to(worktree_root)
                for name in dirnames:
                    child = current_path / name
                    if child.is_symlink():
                        return None
                    if relative_parent.as_posix() == state_dir and name in {
                        "workflow",
                        "metrics",
                    }:
                        continue
                    return None
                for name in filenames:
                    child = current_path / name
                    if child.is_symlink() or not child.is_file():
                        return None
                    rel = (relative_parent / name).relative_to(state_dir)
                    if relative_parent.as_posix() == state_dir and name == _APP_STATE_FILE:
                        continue
                    if relative_parent.as_posix() == f"{state_dir}/workflow" and rel.as_posix() in {
                        f"workflow/{record.feature_id}.json",
                        f"workflow/{record.feature_id}.lock",
                    }:
                        continue
                    if relative_parent.as_posix() == f"{state_dir}/metrics" and rel.as_posix() in {
                        f"metrics/{_APP_STATE_FILE}",
                        f"metrics/{_APP_STATE_FILE}.lock",
                    }:
                        continue
                    return None
        except OSError:
            return None
        return tuple(owned)

    def _archive_state(
        self,
        worktree_root: Path,
        record: RoutingRecord,
        archive_root: Path,
        _owned: tuple[Path, ...],
    ) -> None:
        state = worktree_root / record.state_dir
        if not state.exists():
            return
        try:
            archive_root.mkdir(parents=True, exist_ok=True)
            os.chmod(archive_root, 0o700)
            destination = archive_root / record.state_dir
            if destination.exists():
                raise WorktreeError("archive_exists")
            shutil.move(str(state), str(destination))
        except OSError as exc:
            raise WorktreeError("archive_failed") from exc
