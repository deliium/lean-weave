"""Safe application errors; messages must never contain raw config payloads."""

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    ERROR = 1
    USAGE = 2
    CONFIG = 3


class LeanWeaveError(Exception):
    """Expected application failure."""

    exit_code = ExitCode.ERROR


class ConfigError(LeanWeaveError):
    """Invalid or inaccessible application configuration."""

    exit_code = ExitCode.CONFIG


class DependencyUnavailable(LeanWeaveError):
    """Required injected implementation is unavailable."""


class ProviderError(LeanWeaveError):
    """Safe upstream provider or transport failure.

    Messages must contain only a stable code and safe metadata, never SDK
    exception text, prompts, headers, tool payloads, or reasoning content.
    """


class UnsupportedLLMRequest(ProviderError):
    """The request asks for a mode or combination the gateway does not support."""


class InvalidCompletionError(ProviderError):
    """The provider returned a blank, malformed, or invalid structured result."""


class BudgetExceededError(LeanWeaveError):
    """Feature token budget admission denied an outbound request."""


class JournalError(LeanWeaveError):
    """Persistent usage accounting is unreadable, conflicting, or unwritable."""


class ContextError(LeanWeaveError):
    """Base class for safe deterministic-context failures."""


class InvalidContextRequest(ContextError):
    """Malformed or contradictory context selector supplied by the caller."""


class ForbiddenContext(ContextError):
    """Requested context crosses an eligibility, secret, or stage boundary."""


class ContextScanError(ContextError):
    """Repository discovery could not proceed safely."""


class ContextBudgetError(ContextError):
    """The required prompt envelope exceeds the configured stage budget."""


_CODE_INDEX_CODES = frozenset(
    {
        "root_unavailable",
        "unsafe_cache",
        "storage_unavailable",
        "busy",
        "missing",
        "incompatible",
        "corrupt",
        "recovery_required",
        "incomplete",
        "capacity_exceeded",
        "unsafe_facts",
        "publication_uncertain",
        "clear_incomplete",
        "invalid_request",
        "stale",
        "unknown",
    }
)


class CodeIndexError(LeanWeaveError):
    """Safe persistent-index failure. Messages carry only a stable code."""

    def __init__(self, code: str) -> None:
        if code not in _CODE_INDEX_CODES:
            code = "storage_unavailable"
        self.code = code
        super().__init__(code)


_REPOSITORY_MAP_CODES = frozenset(
    {
        "invalid_selector",
        "not_found",
        "ambiguous_selector",
    }
)

_REPOSITORY_MAP_MESSAGES = {
    "invalid_selector": "Repository map selector is invalid",
    "not_found": "Repository map selector was not found",
    "ambiguous_selector": "Repository map selector is ambiguous",
}


class RepositoryMapError(LeanWeaveError):
    """Safe repository-map selector failure. Messages are fixed, never echo input."""

    def __init__(self, code: str) -> None:
        if code not in _REPOSITORY_MAP_CODES:
            code = "invalid_selector"
        self.code = code
        super().__init__(_REPOSITORY_MAP_MESSAGES[code])


class StateError(LeanWeaveError):
    """Durable workflow state is unreadable, conflicting, or unwritable."""


class StateConflict(StateError):
    """A feature already exists or another writer holds its lease."""


class StateCorrupt(StateError):
    """Persisted workflow state is malformed, incompatible, or out of bounds."""


class WorkflowError(LeanWeaveError):
    """Safe workflow failure. Messages carry stable codes, never payloads."""


class InvalidTransition(WorkflowError):
    """The requested state-machine transition is not allowed."""


class PlanValidationError(WorkflowError):
    """A generated or caller-supplied plan failed policy validation."""


class InstructionError(WorkflowError):
    """An instruction fragment selection is unknown or over budget."""


class WorkflowBudgetExceeded(WorkflowError):
    """A workflow-stage call or token allowance denied dispatch."""


class EffectUnresolved(WorkflowError):
    """An in-flight effect cannot be proven complete and must not replay."""


class ToolError(LeanWeaveError):
    """Safe code-tool failure. Messages are stable codes, never payloads."""


class InvalidToolRequest(ToolError):
    """Malformed tool input. The message must not echo the rejected value."""


class ForbiddenTool(ToolError):
    """The tool request crosses an authorization or credential boundary."""


class WorktreeGitError(LeanWeaveError):
    """Safe worktree Git adapter failure. Messages are stable codes only."""


class UnsupportedRepository(WorktreeGitError):
    """Repository layout or local Git configuration is not supported."""


class GitOperationError(WorktreeGitError):
    """A bounded Git process failed, timed out, or returned unusable output."""


class WorktreeError(LeanWeaveError):
    """Safe worktree lifecycle failure. Messages are stable codes only."""


class WorktreeBusy(WorktreeError):
    """Another cooperative process holds the worktree or routing lease."""


class WorktreeConflict(WorktreeError):
    """Feature ID, branch, path, or registration already conflicts."""


class WorktreeStale(WorktreeError):
    """Registered worktree identity no longer matches the live checkout."""
