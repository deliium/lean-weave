from __future__ import annotations

from typing import Any, Protocol


class StateStore(Protocol):
    """Persist FeatureState under .lean-weave/ (and optional git metadata). Sync."""

    def load(self, feature_id: str) -> Any:
        ...

    def save(self, feature_state: Any) -> None:
        """Never persist secrets or full chat transcripts."""
        ...
