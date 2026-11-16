from __future__ import annotations

from typing import Protocol, Sequence


class CLI(Protocol):
    """User-facing entrypoint. Sync. No LLM/orchestrator logic beyond dispatch."""

    def run(self, argv: Sequence[str] | None = None) -> int:
        """Parse args, invoke WorkflowOrchestrator, return process exit code."""
        ...
