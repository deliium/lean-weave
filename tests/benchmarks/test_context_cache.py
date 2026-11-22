from __future__ import annotations

from pathlib import Path

from benchmarks.context_cache import ITERATIONS, run_benchmark


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
        assert all(mode["wall_ms_samples"] for mode in modes)
        assert all(mode["cpu_ms_samples"] for mode in modes)
        warm = modes[2]
        assert warm["warm"] is True
        assert warm["hits"] > 0
        assert warm["hits_by_kind"]
        assert warm["calls"].get("rank_local", 0) == 0
        assert warm["calls"].get("pack_build", 0) >= ITERATIONS
        updates = item["index_updates"]
        assert updates["build_ms"] >= 0
        assert updates["unchanged_update_ms"] >= 0
        assert updates["edit_update_ms"] >= 0
        assert updates["clean_rebuild_ms"] >= 0
        assert updates["incremental_matches_clean"] is True
        assert updates["restored"] is True


def test_scaled_workload_is_synthetic_and_deterministic(tmp_path: Path):
    from benchmarks.context_cache import run_scaled_benchmark

    result = run_scaled_benchmark(
        scratch=tmp_path / "scaled",
        sizes=(10, 20),
        dense_imports=8,
        iterations=2,
    )
    assert set(result) == {"scaled_10", "scaled_20", "dense_imports"}
    for item in result.values():
        assert item["synthetic"] is True
        mode = item["modes"][0]
        assert mode["signatures_equal"] is True
        assert mode["wall_ms_samples"]
        assert mode["cpu_ms_samples"]
        assert mode["llm_calls"] == 0
        assert mode["api_tokens_measured"] is False
    assert result["scaled_10"]["offscope_files"] == 10
    assert result["scaled_20"]["offscope_files"] == 20
    assert result["dense_imports"]["dense_imports"] == 8
    assert result["dense_imports"]["modes"][0]["calls"].get("facts_extract", 0) > 0
