"""Retrieval comparison metrics and fixture regressions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lean_weave.benchmark.retrieval import (
    CASES_PATH,
    GIT_SIGNAL_DECOY,
    GIT_SIGNAL_LABELED,
    RankedHit,
    cost_metrics,
    load_cases,
    precision_at_k,
    recall_at_k,
    run_comparison,
    run_git_signal_comparison,
)


def test_cases_are_well_formed():
    cases = load_cases()
    assert cases
    assert CASES_PATH.is_file()
    ids = [case.id for case in cases]
    assert ids == sorted(ids)
    assert any(case.repository == "bug_fix" for case in cases)
    assert any(case.split == "holdout" for case in cases)
    kinds = {case.task_kind for case in cases}
    assert {"bugfix", "feature", "refactor", "test", "diagnose"} <= kinds


def test_precision_recall_helpers():
    ranked = ["a.py", "b.py", "a.py", "c.py"]
    relevant = {"a.py", "c.py"}
    assert precision_at_k(ranked, relevant, 3) == pytest.approx(2 / 3)
    assert recall_at_k(ranked, relevant, 3) == pytest.approx(1.0)
    assert precision_at_k([], relevant, 1) == 0.0


def test_cost_metrics_prefix_and_duplicates():
    hits = [
        RankedHit("a.py", None, None, 1, 2, 100, ()),
        RankedHit("b.py", None, None, 1, 1, 100, ()),
        RankedHit("a.py", None, None, 1, 2, 50, ()),
    ]
    ranges = [{"path": "a.py", "start_line": 1, "end_line": 2}]
    metrics = cost_metrics(hits, ranges)
    assert metrics["cumulative"]["cost@1"] == 100
    assert metrics["cumulative"]["cost@3"] == 250
    assert metrics["prefix_256"]["gold_intervals_covered"] == 1
    assert metrics["prefix_512"]["prefix_hits"] == 3
    assert cost_metrics([], ranges)["gold_range_covered"] is False
    short = cost_metrics(hits[:1], ranges)
    assert short["gold_range_covered"] is True


def test_comparison_runs_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    git_reads: list[str] = []
    real_read = __import__(
        "lean_weave.benchmark.retrieval", fromlist=["read_retrieval_diff"]
    ).read_retrieval_diff

    def tracked_read(root, admitted_paths, **kwargs):
        git_reads.append(Path(root).as_posix())
        return real_read(root, admitted_paths, **kwargs)

    monkeypatch.setattr("lean_weave.benchmark.retrieval.read_retrieval_diff", tracked_read)

    cases_done = {"n": 0}
    from lean_weave.benchmark import retrieval as retrieval_mod

    real_evaluate = retrieval_mod.evaluate_case

    def evaluate_guard(case, scratch):
        before = len(git_reads)
        row = real_evaluate(case, scratch)
        assert len(git_reads) == before, "matched comparison must not read Git diffs"
        cases_done["n"] += 1
        return row

    monkeypatch.setattr(retrieval_mod, "evaluate_case", evaluate_guard)

    report = run_comparison(as_json=False)
    assert cases_done["n"] == len(report["cases"])
    assert git_reads, "git_signal should exercise the production diff adapter"
    assert report["schema"] == "lw-retrieval-comparison-v1"
    assert report["cases"]
    assert report["v1_prompt_budget_tokens"] == 2048
    assert report["cost_prefix_budgets"] == [256, 512]

    bug = next(item for item in report["cases"] if item["id"] == "bug_fix_add")
    assert bug["v2_cold"]["files"][0] == "src/calc.py" or "src/calc.py" in bug["v2_cold"]["files"][:5]
    assert bug["v2_warm"]["files"][0] == "src/calc.py" or "src/calc.py" in bug["v2_warm"]["files"][:5]
    assert "cost" in bug["v2_cold"]
    assert "cost@1" in bug["v2_cold"]["cost"]["cumulative"]
    assert "prefix_256" in bug["v2_cold"]["cost"]
    assert bug["v1_prompt"]["observation"] == "v1_prompt_excerpts"
    assert bug["v1_prompt"]["budget_tokens"] == 2048
    assert isinstance(bug["v1_prompt"]["excerpts"], list)

    diagnose = next(item for item in report["cases"] if item["id"] == "holdout_diagnose")
    assert "gold_range_covered" in diagnose["v2_warm"]["cost"]

    git = report["git_signal"]
    assert git["observation"] == "git_signal_dirty_checkout"
    assert git["production_diff"]["labeled_has_git_diff"] is True
    assert git["production_diff"]["decoy_has_git_diff"] is False
    assert git["production_diff"]["decoy_present"] is False
    assert GIT_SIGNAL_LABELED in git["production_diff"]["files"]
    assert GIT_SIGNAL_DECOY not in git["read_diff_none"]["files"]

    encoded = json.dumps(report, sort_keys=True)
    assert "XDG_CACHE_HOME" not in encoded
    assert str(tmp_path) not in encoded

    again = run_comparison(as_json=False)
    assert again["macro"] == report["macro"]
    assert [item["id"] for item in again["cases"]] == [item["id"] for item in report["cases"]]


def test_git_signal_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    result = run_git_signal_comparison(tmp_path)
    assert result["production_diff"]["labeled_has_git_diff"] is True
    assert result["read_diff_none"]["labeled_has_git_diff"] is False
    assert result["production_diff"]["decoy_has_git_diff"] is False
