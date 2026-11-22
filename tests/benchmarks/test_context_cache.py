from __future__ import annotations

from pathlib import Path

from benchmarks.context_cache import run_benchmark


def test_benchmark_reports_modes_and_reuse(tmp_path: Path):
    result = run_benchmark(scratch=tmp_path / "bench")
    assert result
    for workload, item in result.items():
        modes = item["modes"]
        assert len(modes) == 3
        assert all(mode["signatures_equal"] for mode in modes)
        assert len({mode["signature"] for mode in modes}) == 1
        assert all(mode["llm_calls"] == 0 for mode in modes)
        assert all(mode["api_tokens_measured"] is False for mode in modes)
        warm = modes[2]
        assert warm["warm"] is True
        assert warm["hits"] > 0
        assert warm["calls"].get("rank_local", 0) == 0
