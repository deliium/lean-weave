from __future__ import annotations

from typing import Any, Protocol


class WorkflowOrchestrator(Protocol):
    """Drive plan→implement→verify→retry via minimal state machine. Sync MVP.

    Forbidden dependencies: MCP/ACP, Docker APIs, background workers,
    multi-agent routers, nested /aif-* skill launches.
    """

    def run_feature(self, feature_id: str, request: str) -> Any:
        """Execute until done/failed/interrupt; persist FeatureState."""
        ...

    def resume(self, feature_id: str, decision: Any | None = None) -> Any:
        """Resume after interrupt or error."""
        ...
