from __future__ import annotations

from typing import Any, Protocol

from lean_weave.schemas import FeatureState


class WorkflowOrchestrator(Protocol):
    """Drive plan→implement→verify→retry via minimal state machine. Sync MVP.

    Forbidden dependencies: MCP/ACP, Docker APIs, background workers,
    multi-agent routers, nested /aif-* skill launches.
    """

    def run_feature(self, feature_id: str, request: str) -> FeatureState:
        """Execute until done/failed/interrupt; persist FeatureState."""
        ...

    def resume(self, feature_id: str, decision: Any | None = None) -> FeatureState:
        """Resume after interrupt or error."""
        ...
