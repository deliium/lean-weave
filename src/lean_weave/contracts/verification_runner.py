from __future__ import annotations

from typing import Protocol

from lean_weave.schemas import FeatureState, VerificationResult


class VerificationRunner(Protocol):
    """Deterministic verify gates. Sync. Prefer no LLM in MVP 1."""

    def run(self, feature_state: FeatureState) -> VerificationResult:
        """Return VerificationResult (pass/warn/fail) with gate details."""
        ...
