"""Thin feature-session composition over worktrees and the orchestrator.

Owns provisioning, operator-config provenance, frozen approvals, and read-only
projection. Does not implement a second stage loop or budget ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pathspec

from lean_weave.app import AppContext, create_llm_provider, create_orchestrator
from lean_weave.config import AppConfig, load_config
from lean_weave.errors import (
    ConfigError,
    LeanWeaveError,
    WorktreeError,
)
from lean_weave.executor.approval import ToolApproval, approve_tools
from lean_weave.logging_setup import get_logger
from lean_weave.metrics.jsonl import JsonlMetricsCollector
from lean_weave.schemas import FeatureState, Stage
from lean_weave.verify.process import IsolationBackend
from lean_weave.workflow.lifecycle import ExecutionMode, authority_digest, plan_digest
from lean_weave.workflow.context_lifecycle import context_authority_material
from lean_weave.workflow.orchestrator import MinimalWorkflowOrchestrator
from lean_weave.workflow.planning import PlanPolicy
from lean_weave.workflow.results import WorkflowResult, WorkflowStatus, project_result
from lean_weave.workflow.stats import FeatureStats, project_feature_stats
from lean_weave.workflow.state import (
    JsonCheckpointStore,
    normalize_feature_id,
    workspace_identity,
)
from lean_weave.worktree.git import preflight_repository, read_status
from lean_weave.worktree.manager import WorktreeManager
from lean_weave.worktree.registry import (
    ProvisioningPhase,
    clear_cancel_marker,
    list_routing_ids,
    read_cancel_marker,
    read_routing,
)

log = get_logger("workflow.session")

MAX_ENUMERATED_WRITES = 4096


@dataclass(frozen=True)
class FeatureView:
    feature_id: str
    status: str
    reason: str
    stage: str
    worktree_path: str | None
    branch: str | None
    next_action: str
    plan_digest: str | None = None
    approval_token: str | None = None
    approval_command: str | None = None
    modified_paths: tuple[str, ...] = ()
    workflow: WorkflowResult | None = None
    plan_goal: str | None = None
    plan_tasks: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
    change_state: str = "unknown"
    change_summary: str = ""
    cancel_requested: bool = False
    archived: bool = False
    archive_path: str | None = None


class FeatureSession:
    """Compose worktree lifecycle with the existing orchestrator factory."""

    def __init__(
        self,
        *,
        source_cwd: Path | None = None,
        llm_provider: Any | None = None,
        backend: IsolationBackend | None = None,
    ) -> None:
        self._source_cwd = (source_cwd or Path.cwd()).resolve()
        self._llm_provider = llm_provider
        self._backend = backend
        self._manager = WorktreeManager(self._source_cwd)

    def start(
        self,
        description: str,
        *,
        mode: Literal["auto", "review"],
        config_path: Path,
        adopt_worktree: Path | None = None,
        check_ids: Sequence[str] | None = None,
        transport: Any | None = None,
        api_key: str | None = None,
        post_settlement_policy: Any | None = None,
    ) -> FeatureView:
        if not description.strip():
            raise ConfigError("feature_description_required")
        if mode not in {"auto", "review"}:
            raise ConfigError("invalid_execution_mode")
        exec_mode = ExecutionMode.AUTO if mode == "auto" else ExecutionMode.REVIEW
        loaded = self._load_operator_config(config_path)
        manager = WorktreeManager(
            self._source_cwd,
            state_dir=loaded.config.workflow.state_dir,
        )
        self._manager = manager
        if adopt_worktree is not None:
            provisioned = manager.adopt(adopt_worktree)
        else:
            provisioned = manager.create()
        feature_id = provisioned.feature_id
        worktree = provisioned.identity.root
        try:
            approval = self._build_approval(worktree, loaded.config, config_path)
            selected_checks = self._select_checks(approval, check_ids)
            policy = PlanPolicy(
                approved_paths=frozenset(approval.write_allow),
                approved_check_ids=frozenset(selected_checks),
            )
            authority = self._authority_digest(
                loaded.config, approval, selected_checks
            )
            baseline = self._fingerprint(worktree, approval.write_allow)
            if loaded.config.context.workflow_mode == "v1":
                baseline_callback = lambda: baseline
            else:
                baseline_callback = lambda: self._fingerprint(
                    worktree, approval.write_allow
                )
            store = JsonCheckpointStore(worktree, state_dir=loaded.config.workflow.state_dir)
            journal = JsonlMetricsCollector.from_config(loaded.config, root=worktree)
            context = AppContext(
                config=loaded.config,
                config_source=loaded.source,
                llm_provider=self._llm_provider,
                state_store=store,
                metrics_collector=journal,
            )
            if (
                self._llm_provider is None
                and (transport is not None or post_settlement_policy is not None)
            ):
                context.llm_provider = create_llm_provider(
                    context,
                    journal=journal,
                    transport=transport,
                    api_key=api_key or ("sk-offline-test" if transport is not None else None),
                    post_settlement_policy=post_settlement_policy,
                )
            common = manager.source.common_dir

            def cancel_check() -> bool:
                return read_cancel_marker(common, feature_id) is not None

            orchestrator = create_orchestrator(
                context,
                approval=approval,
                backend=self._backend,
                check_ids=selected_checks,
                plan_policy=policy,
                journal=journal,
                workspace_root=worktree,
                orchestrator_kwargs={
                    "mode": exec_mode,
                    "pinned_head": provisioned.identity.head,
                    "baseline_fingerprint": baseline_callback,
                    "binding_extra": lambda: {
                        "ownership": provisioned.record.ownership.value,
                        "branch": provisioned.record.branch,
                        "worktree": str(worktree),
                        "authority": authority,
                    },
                    "cancel_check": cancel_check,
                    "authority_digest": authority,
                },
            )
            assert isinstance(orchestrator, MinimalWorkflowOrchestrator)
            with manager.lease(feature_id):
                state = orchestrator.run_feature(feature_id, description.strip())
                workflow = orchestrator.result(feature_id)
                if workflow.status is WorkflowStatus.CANCELLED:
                    manager.consume_cancel_marker(feature_id)
            return self._view_from_state(
                feature_id,
                state,
                workflow,
                config_path=config_path,
            )
        except Exception:
            log.warning("[Session.start] feature=%s outcome=failed", feature_id)
            raise

    def resume(
        self,
        feature_id: str,
        *,
        config_path: Path | None = None,
        approve_token: str | None = None,
        check_ids: Sequence[str] | None = None,
    ) -> FeatureView:
        safe = normalize_feature_id(feature_id)
        record = self._manager.lookup(safe)
        if record.phase != ProvisioningPhase.READY:
            if record.phase in {
                ProvisioningPhase.ARCHIVED,
                ProvisioningPhase.TOMBSTONE,
            }:
                return self._read_only_view(safe)
            raise WorktreeError("feature_not_ready")
        provisioned = self._manager.revalidate(safe)
        record = provisioned.record
        worktree = provisioned.identity.root
        # Terminal / inspection-only resume without config.
        store = JsonCheckpointStore(worktree, state_dir=record.state_dir)
        try:
            envelope = store.load_checkpoint(safe)
        except LeanWeaveError:
            raise WorktreeError("checkpoint_missing") from None
        if envelope.state.stage in {Stage.DONE, Stage.FAILED}:
            self._clear_stale_marker(safe)
            return self._view_from_state(
                safe,
                envelope.state,
                project_result(envelope.state, envelope.control),
                config_path=config_path,
            )
        if read_cancel_marker(self._manager.source.common_dir, safe) is not None:
            # A durable cancel wins over a new dispatch when the owner is idle.
            return self._finalize_idle_cancel(safe)
        if config_path is None:
            raise ConfigError("operator_config_required")
        loaded = self._load_operator_config(config_path, worktree=worktree)
        if loaded.config.workflow.state_dir != record.state_dir:
            log.warning(
                "[Session.resume] feature=%s outcome=state_dir_drift",
                safe,
            )
            raise ConfigError("state_dir_drift")
        approval = self._build_approval(worktree, loaded.config, config_path)
        selected_checks = self._select_checks(approval, check_ids)
        policy = PlanPolicy(
            approved_paths=frozenset(approval.write_allow),
            approved_check_ids=frozenset(selected_checks),
        )
        authority = self._authority_digest(
            loaded.config, approval, selected_checks
        )
        # Recompute the live fingerprint at acceptance time. Reusing the saved
        # value would make every post-pause edit silently acceptable.
        baseline = self._fingerprint(worktree, approval.write_allow)
        # Journal must follow the frozen routing state_dir, never a drifted config.
        journal = JsonlMetricsCollector((worktree / record.state_dir).resolve())
        context = AppContext(
            config=loaded.config,
            config_source=loaded.source,
            llm_provider=self._llm_provider,
            state_store=store,
            metrics_collector=journal,
        )
        common = self._manager.source.common_dir

        def cancel_check() -> bool:
            return read_cancel_marker(common, safe) is not None

        orchestrator = create_orchestrator(
            context,
            approval=approval,
            backend=self._backend,
            check_ids=selected_checks,
            plan_policy=policy,
            journal=journal,
            workspace_root=worktree,
            orchestrator_kwargs={
                "mode": envelope.control.mode,
                "pinned_head": record.base_commit,
                "baseline_fingerprint": lambda: self._fingerprint(
                    worktree, approval.write_allow
                ),
                "binding_extra": lambda: {
                    "ownership": record.ownership.value,
                    "branch": record.branch,
                    "worktree": str(worktree),
                    "authority": authority,
                },
                "cancel_check": cancel_check,
                "authority_digest": authority,
            },
        )
        assert isinstance(orchestrator, MinimalWorkflowOrchestrator)
        decision = None
        if approve_token is not None:
            decision = {"kind": "approve_review", "token": approve_token}
        with self._manager.lease(safe):
            current = read_routing(self._manager.source.common_dir, safe)
            if read_cancel_marker(self._manager.source.common_dir, safe) is not None:
                return self._finalize_cancelled_locked(current)
            state = orchestrator.resume(safe, decision)
            workflow = orchestrator.result(safe)
            if workflow.status is WorkflowStatus.CANCELLED:
                self._manager.consume_cancel_marker(safe)
        return self._view_from_state(
            safe,
            state,
            workflow,
            config_path=config_path,
        )

    def status(self, feature_id: str) -> FeatureView:
        return self._read_only_view(feature_id)

    def result(self, feature_id: str) -> FeatureView:
        return self._read_only_view(feature_id)

    def stats(self, feature_id: str) -> FeatureStats:
        """Read one feature's journal and checkpoint without side effects."""

        safe = normalize_feature_id(feature_id)
        return self._stats_once(safe, retry=True)

    def stats_all(self) -> tuple[list[FeatureStats], list[dict[str, str]]]:
        """Enumerate routed features in sorted order without a repo-wide scan."""

        ids = list_routing_ids(self._manager.source.common_dir)
        features: list[FeatureStats] = []
        failures: list[dict[str, str]] = []
        for feature_id in ids:
            try:
                features.append(self._stats_once(feature_id, retry=True))
            except LeanWeaveError as exc:
                failures.append(
                    {
                        "feature_id": feature_id,
                        "reason": str(exc)[:64] or "stats_unavailable",
                    }
                )
        log.debug(
            "[Session.stats] available=%s failed=%s",
            len(features),
            len(failures),
        )
        return features, failures

    def _stats_once(self, feature_id: str, *, retry: bool) -> FeatureStats:
        record = self._manager.lookup(feature_id)
        root = Path(record.worktree_root)
        archived = False
        expected: str | None = None
        if record.phase in {ProvisioningPhase.ARCHIVED, ProvisioningPhase.TOMBSTONE}:
            if not record.archive_root:
                raise WorktreeError("archive_missing")
            root = Path(record.archive_root)
            expected = workspace_identity(Path(record.worktree_root))
            archived = True
        store = JsonCheckpointStore(
            root,
            state_dir=record.state_dir,
            expected_workspace=expected,
        )
        envelope = store.load_checkpoint(feature_id)
        journal = JsonlMetricsCollector((root / record.state_dir).resolve())
        snapshot = journal.reporting_snapshot(feature_id)
        workflow = project_result(envelope.state, envelope.control)
        result = project_feature_stats(
            feature_id=feature_id,
            state=envelope.state,
            control=envelope.control,
            workflow=workflow,
            snapshot=snapshot,
            archived=archived,
        )
        if retry:
            # The route, checkpoint and journal are independent files. If the
            # route moved during collection, discard the mixed snapshot once.
            try:
                current = read_routing(self._manager.source.common_dir, feature_id)
                if (
                    current.worktree_root != record.worktree_root
                    or current.state_dir != record.state_dir
                ):
                    log.warning(
                        "[Session.stats] feature=%s outcome=route_changed", feature_id
                    )
                    return self._stats_once(feature_id, retry=False)
            except LeanWeaveError:
                if not archived:
                    raise
        return result

    def cancel(self, feature_id: str) -> FeatureView:
        safe = normalize_feature_id(feature_id)
        outcome = self._manager.request_cancel(safe)
        if outcome == "idle":
            # Idle: finalize under lease without provider construction.
            with self._manager.lease(safe) as record:
                return self._finalize_cancelled_locked(record)
        if outcome == "requested":
            log.info("[Session.cancel] feature=%s outcome=requested", safe)
        return self._read_only_view(safe)

    def _finalize_idle_cancel(self, feature_id: str) -> FeatureView:
        with self._manager.lease(feature_id) as record:
            return self._finalize_cancelled_locked(record)

    def _finalize_cancelled_locked(self, record) -> FeatureView:
        safe = record.feature_id
        if record.phase != ProvisioningPhase.READY:
            return self._read_only_view(safe)
        store = JsonCheckpointStore(Path(record.worktree_root), state_dir=record.state_dir)
        try:
            envelope = store.load_checkpoint(safe)
        except LeanWeaveError:
            return self._read_only_view(safe)
        if envelope.state.stage in {Stage.DONE, Stage.FAILED}:
            clear_cancel_marker(self._manager.source.common_dir, safe)
            return self._view_from_state(
                safe,
                envelope.state,
                project_result(envelope.state, envelope.control),
            )
        if envelope.control.pending_effect is not None:
            raise WorktreeError("cancel_requires_inspection")
        envelope.state.stage = Stage.FAILED
        envelope.control.terminal_status = WorkflowStatus.CANCELLED
        envelope.control.terminal_reason = "cancelled"
        envelope.control.inflight_stage = None
        store.save_checkpoint(envelope.state, envelope.control)
        clear_cancel_marker(self._manager.source.common_dir, safe)
        log.info("[Session.cancel] feature=%s outcome=idle_terminal", safe)
        return self._view_from_state(
            safe,
            envelope.state,
            project_result(envelope.state, envelope.control),
        )

    def _clear_stale_marker(self, feature_id: str) -> None:
        marker = read_cancel_marker(self._manager.source.common_dir, feature_id)
        if marker is None:
            return
        try:
            with self._manager.lease(feature_id):
                clear_cancel_marker(self._manager.source.common_dir, feature_id)
        except LeanWeaveError:
            log.debug("[Session.cancel] feature=%s outcome=marker_retained", feature_id)

    def inspect_worktree(self, feature_id: str) -> Mapping[str, Any]:
        inspection = self._manager.inspect(feature_id)
        return {
            "feature_id": inspection.feature_id,
            "ownership": inspection.ownership.value,
            "phase": inspection.phase.value,
            "worktree_root": str(inspection.worktree_root),
            "branch": inspection.branch,
            "cancel_requested": inspection.cancel_requested,
            "reason": inspection.reason,
            "dirty_count": 0 if inspection.status is None else len(inspection.status.entries),
        }

    def cleanup_worktree(self, feature_id: str, *, confirm: bool) -> Mapping[str, Any]:
        record = self._manager.cleanup(feature_id, confirm=confirm)
        return {
            "feature_id": record.feature_id,
            "phase": record.phase.value,
            "archive_root": record.archive_root,
        }

    def _read_only_view(self, feature_id: str) -> FeatureView:
        safe = normalize_feature_id(feature_id)
        record = self._manager.lookup(safe)
        worktree = Path(record.worktree_root)
        workflow = None
        state = None
        archived = False
        archive_path = record.archive_root
        root = worktree
        expected = None
        if archive_path and record.phase in {
            ProvisioningPhase.ARCHIVED,
            ProvisioningPhase.TOMBSTONE,
        }:
            root = Path(archive_path)
            expected = workspace_identity(worktree)
            archived = True
        if record.phase in {ProvisioningPhase.READY, ProvisioningPhase.ARCHIVED, ProvisioningPhase.TOMBSTONE}:
            try:
                store = JsonCheckpointStore(
                    root,
                    state_dir=record.state_dir,
                    expected_workspace=expected,
                )
                envelope = store.load_checkpoint(safe)
                state = envelope.state
                workflow = project_result(envelope.state, envelope.control)
            except LeanWeaveError:
                workflow = None
        modified, change_state, change_summary = self._changes(record, archived=archived)
        cancel_requested = (
            read_cancel_marker(self._manager.source.common_dir, safe) is not None
        )
        if state is None:
            return FeatureView(
                feature_id=safe,
                status=record.phase.value,
                reason="routing_only",
                stage="unknown",
                worktree_path=str(worktree),
                branch=record.branch,
                next_action=self._next_action(record.phase, None, None),
                modified_paths=modified,
                change_state=change_state,
                change_summary=change_summary,
                cancel_requested=cancel_requested,
                archived=archived,
                archive_path=archive_path,
            )
        return self._view_from_state(
            safe,
            state,
            workflow,
            modified=modified,
            branch=record.branch,
            worktree_path=str(worktree),
            change_state=change_state,
            change_summary=change_summary,
            cancel_requested=cancel_requested,
            archived=archived,
            archive_path=archive_path,
        )

    def _changes(
        self, record, *, archived: bool
    ) -> tuple[tuple[str, ...], str, str]:
        if archived:
            return (), "archived", "live diff unavailable"
        if record.phase != ProvisioningPhase.READY:
            return (), "unavailable", "workspace unavailable"
        try:
            live = preflight_repository(Path(record.worktree_root))
            status = read_status(live, include_ignored=False)
        except LeanWeaveError:
            return (), "unavailable", "diff unavailable"
        if not status.entries:
            return (), "clean", "staged:0,unstaged:0,untracked:0"
        staged = unstaged = untracked = 0
        for entry in status.entries:
            if entry.xy == "??":
                untracked += 1
                continue
            if entry.xy[0] not in {" ", "?"}:
                staged += 1
            if entry.xy[1] not in {" ", "?"}:
                unstaged += 1
        paths = tuple(sorted({entry.path for entry in status.entries}))
        summary = f"staged:{staged},unstaged:{unstaged},untracked:{untracked}"
        return paths, "modified", summary

    def _view_from_state(
        self,
        feature_id: str,
        state: FeatureState,
        workflow: WorkflowResult | None,
        *,
        config_path: Path | None = None,
        modified: tuple[str, ...] = (),
        branch: str | None = None,
        worktree_path: str | None = None,
        change_state: str = "unknown",
        change_summary: str = "",
        cancel_requested: bool = False,
        archived: bool = False,
        archive_path: str | None = None,
    ) -> FeatureView:
        record = None
        try:
            record = self._manager.lookup(feature_id)
            branch = branch or record.branch
            worktree_path = worktree_path or record.worktree_root
            archive_path = archive_path or record.archive_root
        except LeanWeaveError:
            pass
        if record is not None and change_state == "unknown":
            modified, change_state, change_summary = self._changes(
                record, archived=archived
            )
        if record is not None:
            cancel_requested = cancel_requested or (
                read_cancel_marker(self._manager.source.common_dir, feature_id) is not None
            )
        status = workflow.status.value if workflow is not None else state.stage.value
        reason = workflow.reason if workflow is not None else ""
        token = None
        digest = None
        command = None
        store_root = Path(worktree_path) if worktree_path else None
        if archived and archive_path:
            store_root = Path(archive_path)
        if store_root is not None and record is not None:
            try:
                expected = (
                    workspace_identity(Path(record.worktree_root))
                    if archived
                    else None
                )
                envelope = JsonCheckpointStore(
                    store_root,
                    state_dir=record.state_dir,
                    expected_workspace=expected,
                ).load_checkpoint(feature_id)
                gate = envelope.control.review_gate
                if gate is not None and not gate.accepted and state.plan is not None:
                    digest = gate.plan_digest
                    token = gate.token
                    if token is not None:
                        command = (
                            f"wl --config {config_path} resume {feature_id} --approve {token}"
                            if config_path is not None
                            else f"wl --config <operator-config> resume {feature_id} --approve {token}"
                        )
            except LeanWeaveError:
                pass
        if digest is None and state.plan is not None:
            digest = plan_digest(state.plan)
        plan_goal = state.plan.goal if state.plan is not None else None
        plan_tasks: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
        if state.plan is not None:
            plan_tasks = tuple(
                (task.id, task.title, tuple(task.files))
                for task in state.plan.tasks[:32]
            )
        return FeatureView(
            feature_id=feature_id,
            status=status,
            reason=reason,
            stage=state.stage.value,
            worktree_path=worktree_path,
            branch=branch,
            next_action=self._next_action(
                record.phase if record is not None else ProvisioningPhase.READY,
                state,
                workflow,
            ),
            plan_digest=digest,
            approval_token=token,
            approval_command=command,
            modified_paths=modified,
            workflow=workflow,
            plan_goal=plan_goal,
            plan_tasks=plan_tasks,
            change_state=change_state,
            change_summary=change_summary,
            cancel_requested=cancel_requested,
            archived=archived,
            archive_path=archive_path,
        )

    def _next_action(
        self,
        phase: ProvisioningPhase,
        state: FeatureState | None,
        workflow: WorkflowResult | None,
    ) -> str:
        if phase in {ProvisioningPhase.TOMBSTONE, ProvisioningPhase.ARCHIVED}:
            return "none"
        if workflow is not None:
            if workflow.status is WorkflowStatus.SUCCESS:
                return "result"
            if workflow.status is WorkflowStatus.CANCELLED:
                return "result"
            if workflow.status is WorkflowStatus.INTERRUPTED:
                if workflow.reason == "review_pending":
                    return "resume_with_approve"
                if workflow.reason in {"unresolved_effect", "committed_unconfirmed"}:
                    return "inspect"
                return "resume"
            if workflow.status in {WorkflowStatus.FAILED, WorkflowStatus.BUDGET_EXHAUSTED}:
                if workflow.reason in {"manual_inspection", "unresolved_effect", "committed_unconfirmed"}:
                    return "inspect"
                return "result"
        if state is not None and state.stage is Stage.INTERRUPTED:
            return "resume"
        return "status"

    def _load_operator_config(self, config_path: Path, *, worktree: Path | None = None):
        resolved = self._validate_operator_config_path(config_path, worktree=worktree)
        loaded = load_config(resolved, required=True)
        log.debug("[Session.config] outcome=loaded")
        return loaded

    def _validate_operator_config_path(
        self, config_path: Path, *, worktree: Path | None = None
    ) -> Path:
        try:
            resolved = config_path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ConfigError("operator_config_unreadable") from exc
        if not resolved.is_file() or resolved.is_symlink():
            # Reject symlink config files for provenance clarity.
            try:
                info = os.lstat(resolved)
            except OSError as exc:
                raise ConfigError("operator_config_unreadable") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ConfigError("operator_config_invalid")
        source = self._manager.source.root
        common = self._manager.source.common_dir
        forbidden = [source, common]
        if worktree is not None:
            forbidden.append(worktree.resolve())
        for boundary in forbidden:
            try:
                if resolved.is_relative_to(boundary.resolve()):
                    log.warning("[Session.config] outcome=config_inside_checkout")
                    raise ConfigError("operator_config_inside_checkout")
            except (OSError, RuntimeError, ValueError) as exc:
                raise ConfigError("operator_config_invalid") from exc
        return resolved

    def _build_approval(
        self, worktree: Path, config: AppConfig, config_path: Path
    ) -> ToolApproval:
        exact = self._enumerate_write_targets(
            worktree,
            config.tools.write_allow,
            state_dir=config.workflow.state_dir,
        )
        # Freeze exact paths into a temporary config copy for plan policy alignment.
        narrowed = config.model_copy(
            update={
                "tools": config.tools.model_copy(update={"write_allow": list(exact)}),
            }
        )
        protected = list(narrowed.tools.excludes)
        protected.extend(
            [
                ".git",
                narrowed.workflow.state_dir,
                str(Path(narrowed.workflow.state_dir) / "**"),
            ]
        )
        # Map relative custom config references into protected paths when present.
        try:
            rel = config_path.resolve().name
            protected.append(rel)
        except (OSError, RuntimeError, ValueError):
            pass
        narrowed = narrowed.model_copy(
            update={
                "tools": narrowed.tools.model_copy(update={"excludes": protected}),
            }
        )
        approval = approve_tools(
            worktree,
            narrowed,
            allow_writes=True,
            allow_checks=True,
            config_path=config_path,
        )
        log.debug(
            "[Session.approval] writes=%s checks=%s",
            len(approval.write_allow),
            len(approval.checks),
        )
        return approval

    def _enumerate_write_targets(
        self, root: Path, patterns: Sequence[str], *, state_dir: str = ".lean-weave"
    ) -> list[str]:
        if not patterns:
            raise ConfigError("write_allow_empty")
        try:
            spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns)
        except (TypeError, ValueError) as exc:
            raise ConfigError("write_allow_invalid") from exc
        state_parts = Path(state_dir).parts
        matched: list[str] = []
        try:
            for current, dirnames, filenames in os.walk(root, followlinks=False):
                current_path = Path(current)
                if current_path.is_symlink():
                    raise ConfigError("write_allow_incomplete")
                relative_current = current_path.relative_to(root)
                # Prune VCS and configured state directories.
                pruned: list[str] = []
                for name in dirnames:
                    child = current_path / name
                    if child.is_symlink():
                        raise ConfigError("write_allow_incomplete")
                    if name == ".git":
                        continue
                    if (relative_current / name).parts[: len(state_parts)] == state_parts:
                        continue
                    pruned.append(name)
                dirnames[:] = pruned
                for name in filenames:
                    path = current_path / name
                    if path.is_symlink():
                        continue
                    rel = path.relative_to(root).as_posix()
                    if spec.match_file(rel):
                        matched.append(rel)
                        if len(matched) > MAX_ENUMERATED_WRITES:
                            raise ConfigError("write_allow_incomplete")
        except OSError as exc:
            raise ConfigError("write_allow_incomplete") from exc
        matched = sorted(set(matched))
        if not matched:
            raise ConfigError("write_allow_empty")
        return matched

    def _authority_digest(
        self,
        config: AppConfig,
        approval: ToolApproval,
        check_ids: Sequence[str],
    ) -> str:
        checks = [
            {
                "id": check.id,
                "executable": check.executable,
                "argv": list(check.argv),
                "timeout_s": check.timeout_s,
                "output_bytes_max": check.output_bytes_max,
                "runs_workspace_code": check.runs_workspace_code,
            }
            for check in approval.checks
        ]
        return authority_digest(
            write_allow=approval.write_allow,
            checks=checks,
            check_ids=check_ids,
            state_dir=config.workflow.state_dir,
            max_retries=config.workflow.max_retries,
            max_task_tokens=config.workflow.max_task_tokens,
            feature_token_budget=config.workflow.feature_token_budget,
            max_task_llm_calls=config.workflow.max_task_llm_calls,
            context_material=(
                context_authority_material(config, approval, check_ids)
                if config.context.workflow_mode != "v1"
                else None
            ),
        )

    def _select_checks(
        self, approval: ToolApproval, check_ids: Sequence[str] | None
    ) -> tuple[str, ...]:
        available = {check.id: check for check in approval.checks}
        if not available:
            raise ConfigError("checks_required")
        if check_ids is None:
            selected = tuple(sorted(available))
        else:
            selected = tuple(check_ids)
            missing = [item for item in selected if item not in available]
            if missing:
                raise ConfigError("unknown_check_id")
        # Public CLI: refuse workspace-code checks without an injected backend.
        for check_id in selected:
            check = available[check_id]
            if check.runs_workspace_code and self._backend is None:
                raise ConfigError("workspace_code_backend_required")
        return selected

    def _fingerprint(self, root: Path, paths: Sequence[str]) -> str:
        digest = hashlib.sha256()
        for relative in sorted(paths):
            path = root / relative
            try:
                info = os.lstat(path)
            except OSError as exc:
                raise ConfigError("fingerprint_incomplete") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ConfigError("fingerprint_incomplete")
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return "sha256:" + digest.hexdigest()
