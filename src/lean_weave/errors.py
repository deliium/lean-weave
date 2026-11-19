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


class ToolError(LeanWeaveError):
    """Safe code-tool failure. Messages are stable codes, never payloads."""


class InvalidToolRequest(ToolError):
    """Malformed tool input. The message must not echo the rejected value."""


class ForbiddenTool(ToolError):
    """The tool request crosses an authorization or credential boundary."""
