"""Offline Git tests for WorktreeManager lifecycle."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lean_weave.errors import WorktreeBusy, WorktreeConflict, WorktreeError
from lean_weave.worktree.manager import WorktreeManager
from lean_weave.worktree.registry import (
    Ownership,
    ProvisioningPhase,
    exclusive_lease,
    feature_lock_path,
    read_routing,
)


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "PATH": "/usr/bin:/bin",
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
            "HOME": str(root),
        },
    )


def _init_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _run(root, "init", "-q")
    _run(root, "config", "user.email", "fixture@example.invalid")
    _run(root, "config", "user.name", "Fixture")
    (root / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    (root / "README").write_text("base\n", encoding="utf-8")
    _run(root, "add", "tracked.py", "README")
    _run(root, "commit", "-qm", "base")
    return root


def _snapshot(origin: Path) -> dict[str, object]:
    return {
        "tracked": (origin / "tracked.py").read_bytes(),
        "readme": (origin / "README").read_bytes(),
        "head": _run(origin, "rev-parse", "HEAD").stdout,
        "index": (origin / ".git" / "index").read_bytes(),
        "status": _run(origin, "status", "--porcelain=v1", "-uall").stdout,
    }


def test_create_isolates_and_preserves_dirty_origin(tmp_path: Path):
    origin = _init_repo(tmp_path / "origin")
    (origin / "tracked.py").write_text("value = dirty\n", encoding="utf-8")
    (origin / "staged.py").write_text("staged\n", encoding="utf-8")
    _run(origin, "add", "staged.py")
    (origin / "untracked.py").write_text("loose\n", encoding="utf-8")
    before = _snapshot(origin)

    manager = WorktreeManager(origin)
    first = manager.create()
    second = manager.create()
    assert first.feature_id != second.feature_id
    assert first.record.ownership == Ownership.MANAGED
    assert first.record.phase == ProvisioningPhase.READY
    assert first.identity.root != origin.resolve()
    assert (first.identity.root / "tracked.py").read_text(encoding="utf-8") == "value = 1\n"
    assert not (first.identity.root / "untracked.py").exists()
    assert not (first.identity.root / "staged.py").exists()

    after = _snapshot(origin)
    assert after == before


def test_adopt_clean_linked_worktree(tmp_path: Path):
    origin = _init_repo(tmp_path / "origin")
    linked = tmp_path / "manual-wt"
    _run(origin, "worktree", "add", "-b", "manual-branch", str(linked), "HEAD")
    manager = WorktreeManager(origin)
    adopted = manager.adopt(linked)
    assert adopted.record.ownership == Ownership.ADOPTED
    assert adopted.record.branch == "manual-branch"
    assert adopted.identity.root == linked.resolve()

    with pytest.raises(WorktreeConflict, match="primary_checkout_forbidden"):
        manager.adopt(origin)


def test_adopt_refuses_dirty_and_foreign(tmp_path: Path):
    origin = _init_repo(tmp_path / "origin")
    other = _init_repo(tmp_path / "other")
    linked = tmp_path / "dirty-wt"
    _run(origin, "worktree", "add", "-b", "dirty-branch", str(linked), "HEAD")
    (linked / "tracked.py").write_text("changed\n", encoding="utf-8")
    manager = WorktreeManager(origin)
    with pytest.raises(WorktreeError, match="adopt_dirty"):
        manager.adopt(linked)
    with pytest.raises(WorktreeConflict, match="foreign_repository"):
        manager.adopt(other)


def test_inspect_and_cleanup_clean_never_started(tmp_path: Path):
    origin = _init_repo(tmp_path / "origin")
    manager = WorktreeManager(origin)
    provisioned = manager.create()
    feature_id = provisioned.feature_id
    branch = provisioned.record.branch

    inspection = manager.inspect(feature_id)
    assert inspection.phase == ProvisioningPhase.READY
    assert inspection.live is not None
    assert inspection.status is not None

    with pytest.raises(WorktreeError, match="cleanup_unconfirmed"):
        manager.cleanup(feature_id, confirm=False)

    tombstone = manager.cleanup(feature_id, confirm=True)
    assert tombstone.phase == ProvisioningPhase.TOMBSTONE
    assert not Path(provisioned.record.worktree_root).exists()
    # Branch is retained.
    assert _run(origin, "show-ref", "--verify", f"refs/heads/{branch}").returncode == 0


def test_cleanup_refuses_dirty_including_ignored(tmp_path: Path):
    origin = _init_repo(tmp_path / "origin")
    manager = WorktreeManager(origin)
    provisioned = manager.create()
    wt = Path(provisioned.record.worktree_root)
    (wt / ".gitignore").write_text("ignored.bin\n", encoding="utf-8")
    _run(wt, "add", ".gitignore")
    _run(wt, "commit", "-qm", "ignore")
    (wt / "ignored.bin").write_bytes(b"\x00")
    with pytest.raises(WorktreeError, match="cleanup_dirty"):
        manager.cleanup(provisioned.feature_id, confirm=True)


def test_busy_lease_and_duplicate_path_conflict(tmp_path: Path):
    origin = _init_repo(tmp_path / "origin")
    manager = WorktreeManager(origin)
    provisioned = manager.create()
    lock = feature_lock_path(manager.source.common_dir, provisioned.feature_id)
    with exclusive_lease(lock):
        with pytest.raises(WorktreeBusy, match="lease_busy"):
            with manager.lease(provisioned.feature_id):
                pass
        assert manager.request_cancel(provisioned.feature_id) == "requested"
    assert manager.consume_cancel_marker(provisioned.feature_id) is True


def test_failed_add_preserves_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    origin = _init_repo(tmp_path / "origin")
    before = _snapshot(origin)
    manager = WorktreeManager(origin)

    def boom(*_args, **_kwargs):
        raise WorktreeError("injected_add_failure")

    monkeypatch.setattr("lean_weave.worktree.manager.add_worktree", boom)
    with pytest.raises(WorktreeError, match="provision_failed"):
        manager.create()
    assert _snapshot(origin) == before
    # Failed reservation is diagnosable via routing phase.
    from lean_weave.worktree import registry as reg

    ids = reg.list_routing_ids(manager.source.common_dir)
    assert ids
    record = read_routing(manager.source.common_dir, ids[-1])
    assert record.phase == ProvisioningPhase.FAILED


def _seed_checkpoint(
    worktree_root: Path,
    feature_id: str,
    stage,
    *,
    pending: bool = False,
):
    from lean_weave.schemas import FeatureState
    from lean_weave.workflow.state import (
        ControlRecord,
        EffectKind,
        JsonCheckpointStore,
        PendingEffect,
    )

    store = JsonCheckpointStore(worktree_root)
    state = FeatureState(feature_id=feature_id, stage=stage)
    control = ControlRecord(feature_id=feature_id, workspace=store.identity)
    if pending:
        control.pending_effect = PendingEffect(
            kind=EffectKind.PATCH,
            operation_id="patch-1",
            confirmed=False,
        )
    store.create_checkpoint(state, control)


def test_cleanup_refuses_pending_effect_and_active_run(tmp_path: Path):
    from lean_weave.schemas import Stage

    origin = _init_repo(tmp_path / "origin")
    manager = WorktreeManager(origin)

    pending = manager.create()
    _seed_checkpoint(
        Path(pending.record.worktree_root),
        pending.feature_id,
        Stage.FAILED,
        pending=True,
    )
    with pytest.raises(WorktreeError, match="cleanup_pending_effect"):
        manager.cleanup(pending.feature_id, confirm=True)

    active = manager.create()
    _seed_checkpoint(Path(active.record.worktree_root), active.feature_id, Stage.IMPLEMENT)
    with pytest.raises(WorktreeError, match="cleanup_active_run"):
        manager.cleanup(active.feature_id, confirm=True)


def test_cleanup_refuses_unknown_state_artifacts(tmp_path: Path):
    from lean_weave.schemas import Stage

    origin = _init_repo(tmp_path / "origin")
    manager = WorktreeManager(origin)
    provisioned = manager.create()
    root = Path(provisioned.record.worktree_root)
    _seed_checkpoint(root, provisioned.feature_id, Stage.DONE)
    (root / ".lean-weave" / "extra.txt").write_text("unknown\n", encoding="utf-8")

    with pytest.raises(WorktreeError, match="cleanup_dirty"):
        manager.cleanup(provisioned.feature_id, confirm=True)


def test_cleanup_restarts_after_archival_before_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from lean_weave.schemas import Stage

    origin = _init_repo(tmp_path / "origin")
    manager = WorktreeManager(origin)
    provisioned = manager.create()
    root = Path(provisioned.record.worktree_root)
    _seed_checkpoint(root, provisioned.feature_id, Stage.DONE)

    def fail_remove(*_args, **_kwargs):
        raise WorktreeError("remove_failed")

    monkeypatch.setattr("lean_weave.worktree.manager.remove_worktree", fail_remove)
    with pytest.raises(WorktreeError, match="remove_failed"):
        manager.cleanup(provisioned.feature_id, confirm=True)
    record = read_routing(manager.source.common_dir, provisioned.feature_id)
    assert record.phase == ProvisioningPhase.ARCHIVING
    archive = Path(record.archive_root or "")
    assert archive.is_dir()

    monkeypatch.undo()
    final = manager.cleanup(provisioned.feature_id, confirm=True)
    assert final.phase == ProvisioningPhase.TOMBSTONE
    assert final.archive_root is not None
    assert Path(final.archive_root).is_dir()
    assert not root.exists()
