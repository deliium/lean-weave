"""Keep automatic retrieval regressions off the historical holdout catalog."""

from __future__ import annotations

from pathlib import Path

import pytest

DEV_CASES = Path(__file__).resolve().parent / "fixtures" / "retrieval" / "cases.json"
HISTORICAL_CASES = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "lean_weave"
    / "benchmark"
    / "fixtures"
    / "retrieval"
    / "cases.json"
)


@pytest.fixture(autouse=True)
def _release_evidence_dev_retrieval_catalog(monkeypatch):
    """Point load_cases() at the committed development-only catalog."""

    import lean_weave.benchmark.retrieval as retrieval

    monkeypatch.setattr(retrieval, "CASES_PATH", DEV_CASES)
