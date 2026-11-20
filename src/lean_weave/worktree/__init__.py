"""Git worktree lifecycle helpers for isolated feature execution.

The public CLI and workflow session compose these helpers. They do not start
a workflow, open a provider, or authorize tool writes by themselves.
"""

from lean_weave.worktree.git import (
    GitIdentity,
    GitStatusEntry,
    GitStatusResult,
    WorktreeEntry,
    add_worktree,
    list_worktrees,
    preflight_repository,
    read_status,
    remove_worktree,
    resolve_git_executable,
)
from lean_weave.worktree.manager import ProvisionedWorktree, WorktreeInspection, WorktreeManager
from lean_weave.worktree.registry import Ownership, ProvisioningPhase, RoutingRecord

__all__ = [
    "GitIdentity",
    "GitStatusEntry",
    "GitStatusResult",
    "Ownership",
    "ProvisionedWorktree",
    "ProvisioningPhase",
    "RoutingRecord",
    "WorktreeEntry",
    "WorktreeInspection",
    "WorktreeManager",
    "add_worktree",
    "list_worktrees",
    "preflight_repository",
    "read_status",
    "remove_worktree",
    "resolve_git_executable",
]
