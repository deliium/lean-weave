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
