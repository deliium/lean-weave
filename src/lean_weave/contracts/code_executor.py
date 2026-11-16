from __future__ import annotations

from typing import Protocol

from ._types import ExecRequest, ExecResult


class CodeExecutor(Protocol):
    """Local subprocess execution with cwd limits. Sync. Network off by default."""

    def run(self, request: ExecRequest) -> ExecResult:
        """Execute argv; never escalate privileges; redact secrets in logs."""
        ...
