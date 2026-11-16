from __future__ import annotations

from typing import Any, Protocol


class VerificationRunner(Protocol):
    """Deterministic verify gates. Sync. Prefer no LLM in MVP 1."""

    def run(self, feature_state: Any) -> Any:
        """Return VerificationResult (pass/warn/fail) with gate details."""
        ...
