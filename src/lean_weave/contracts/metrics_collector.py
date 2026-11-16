from __future__ import annotations

from typing import Protocol

from ._types import StageName, TokenUsage


class MetricsCollector(Protocol):
    """Record token/usage counters only (ids + integers). Sync."""

    def record_usage(self, feature_id: str, stage: StageName, usage: TokenUsage) -> None:
        ...

    def totals(self, feature_id: str) -> TokenUsage:
        ...
