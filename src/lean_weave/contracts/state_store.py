from __future__ import annotations

from typing import Protocol

from lean_weave.schemas import FeatureState


class StateStore(Protocol):
    """Persist FeatureState under .lean-weave/ (and optional git metadata). Sync."""

    def load(self, feature_id: str) -> FeatureState:
        ...

    def save(self, feature_state: FeatureState) -> None:
        """Never persist secrets or full chat transcripts."""
        ...
