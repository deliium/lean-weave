from __future__ import annotations

from typing import Protocol

from lean_weave.schemas import FeatureState

from ._types import ContextBundle, StageName


class ContextProvider(Protocol):
    """Assemble stage-scoped prompts under token budgets. Sync. Deterministic selection."""

    def build(self, stage: StageName, feature_state: FeatureState) -> ContextBundle:
        """Raise if budgets exceeded or forbidden context classes requested."""
        ...
