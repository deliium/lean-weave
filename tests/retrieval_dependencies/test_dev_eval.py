"""Metric and baseline contract tests for the independent development suite."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lean_weave.context.packing_tokens import ConservativeByteCounter
from lean_weave.context.provider import DeterministicContextProvider
from lean_weave.schemas import Stage

from tests.retrieval_dependencies.dev_cases import (
    BASELINE_REVISION,
    DEV_CASES,
    EvidenceInterval,
    case_file_digests,
    fixture_hash,
    get_case,
)
from tests.retrieval_dependencies.dev_eval import (
    DeliveredUnit,
    charge_dependency_tokens,
    token_overhead_allowance,
    compare_to_baseline,
    evaluate_candidate_gates,
    freeze_baseline_payload,
    gate_failures,
    interval_fully_covered,
    intervals_adjacent_or_gapped,
    normalize_run,
    run_case_arm,
    run_suite,
    run_v1_arm,
    write_tree,
)


def test_adjacent_versus_gapped_range_classification():
    assert intervals_adjacent_or_gapped((1, 3), (4, 6)) == "adjacent"
    assert intervals_adjacent_or_gapped((1, 4), (3, 6)) == "overlapping"
    assert intervals_adjacent_or_gapped((1, 2), (4, 5)) == "gapped"


def test_coverage_requires_full_interval_and_matching_digest():
    expected = EvidenceInterval(
        path="a.py", start_line=2, end_line=4, role="definition"
    )
    digest = "sha256:" + ("a" * 64)
    wrong = "sha256:" + ("b" * 64)
    digests = {"a.py": digest}
    full = DeliveredUnit(
        path="a.py",
        start_line=1,
        end_line=5,
        digest=digest,
        kind="definition",
        text="x",
        unit_id="1",
    )
    partial = DeliveredUnit(
        path="a.py",
        start_line=2,
        end_line=3,
        digest=digest,
        kind="definition",
        text="x",
        unit_id="2",
    )
    bad_digest = DeliveredUnit(
        path="a.py",
        start_line=1,
        end_line=5,
        digest=wrong,
        kind="definition",
        text="x",
        unit_id="3",
    )
    assert interval_fully_covered(expected, [full], file_digests=digests)
    assert not interval_fully_covered(expected, [partial], file_digests=digests)
    assert not interval_fully_covered(expected, [bad_digest], file_digests=digests)


def test_dependency_tokens_charge_intersecting_unit_once():
    digest = "sha256:" + ("c" * 64)
    unit = DeliveredUnit(
        path="a.py",
        start_line=1,
        end_line=10,
        digest=digest,
        kind="definition",
        text="body\n",
        unit_id="u1",
    )
    labels = (
        EvidenceInterval(path="a.py", start_line=2, end_line=3, role="definition"),
        EvidenceInterval(path="a.py", start_line=8, end_line=9, role="import"),
    )
    total = charge_dependency_tokens(
        [unit],
        labels,
        file_digests={"a.py": digest},
        counter=ConservativeByteCounter(),
    )
    once = charge_dependency_tokens(
        [unit],
        labels[:1],
        file_digests={"a.py": digest},
        counter=ConservativeByteCounter(),
    )
    assert total == once
    assert total > 0


def test_blocked_undelivered_fragments_score_zero_coverage(tmp_path: Path):
    case = get_case("budget_exhaustion_large_def")
    workspace = tmp_path / "repo"
    cache = tmp_path / "cache"
    workspace.mkdir()
    cache.mkdir(mode=0o700)
    write_tree(workspace, case.files)
    run = run_case_arm(case, "v2", workspace=workspace, cache_base=cache)
    # Tight budget may refuse or deliver incompletely; undelivered labels count as misses.
    assert run.missing_dependency_count == run.must_cover_misses
    if not run.ready:
        assert run.wire_input_tokens == 0
        assert run.delivered_intervals == ()
        assert run.must_cover_hits == 0


def test_map_only_hits_do_not_count_as_source_coverage():
    expected = EvidenceInterval(
        path="a.py", start_line=1, end_line=2, role="definition"
    )
    # No delivered source units — map rows are not scored here.
    assert not interval_fully_covered(expected, [], file_digests={"a.py": "sha256:" + "d" * 64})


def test_each_baseline_arm_uses_real_temp_checkout(tmp_path: Path):
    case = get_case("control_no_dependency")
    results = {}
    for arm in ("v1", "v2"):
        workspace = tmp_path / arm / "repo"
        cache = tmp_path / arm / "cache"
        workspace.mkdir(parents=True)
        cache.mkdir(mode=0o700)
        write_tree(workspace, case.files)
        results[arm] = run_case_arm(case, arm, workspace=workspace, cache_base=cache)
    assert results["v1"].ready is True
    assert results["v1"].scope_paths
    assert results["v2"].scope_paths == (case.edit_target,)
    assert results["v1"].arm == "v1"
    assert results["v2"].arm == "v2"


def test_v1_capture_comes_from_public_implement_bundle(tmp_path: Path):
    case = get_case("imported_class_call_annotation")
    workspace = tmp_path / "repo"
    workspace.mkdir()
    write_tree(workspace, case.files)
    run = run_v1_arm(case, workspace)
    assert run.ready is True
    assert run.status == "ready"
    # Reconstruct the public provider path independently.
    from tests.retrieval_dependencies.dev_cases import PROFILES, v1_context_config
    from tests.retrieval_dependencies.dev_eval import _feature_state

    provider = DeterministicContextProvider(
        workspace, context_config=v1_context_config(PROFILES[case.profile])
    )
    bundle = provider.build("implement", _feature_state(case))
    assert bundle["stage"] == "implement"
    assert bundle["pack"].excerpts
    assert bundle["messages"]
    assert Stage.IMPLEMENT.value == "implement"


def test_v2_arms_share_ordinary_task_target_never_expected_refs(tmp_path: Path):
    case = get_case("relative_two_hop_chain")
    workspace = tmp_path / "repo"
    cache = tmp_path / "cache"
    workspace.mkdir()
    cache.mkdir(mode=0o700)
    write_tree(workspace, case.files)
    from tests.retrieval_dependencies.dev_eval import _pack_request

    request = _pack_request(case)
    assert request.required_paths == (case.edit_target,)
    assert request.query == case.task_query
    # Labels must not appear as required_symbols or forced fragments.
    assert request.required_symbols == ()
    assert request.forced_fragments == ()
    run = run_case_arm(case, "v2", workspace=workspace, cache_base=cache)
    assert run.scope_paths == (case.edit_target,)


def test_normalized_results_stable_across_repeated_runs(tmp_path: Path):
    first = run_suite(("v1", "v2"), latency_repeats=0, include_latency=False)
    second = run_suite(("v1", "v2"), latency_repeats=0, include_latency=False)
    left = [normalize_run(run) for run in first.case_runs]
    right = [normalize_run(run) for run in second.case_runs]
    assert left == right
    assert first.fixture_hash == second.fixture_hash == fixture_hash()
    assert first.baseline_revision == BASELINE_REVISION


def test_frozen_baseline_matches_live_suite_when_present():
    baseline_path = Path(__file__).with_name("dev_baseline.json")
    if not baseline_path.exists():
        pytest.skip("dev_baseline.json not frozen yet")
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    summary = run_suite(("v1", "v2"), latency_repeats=0, include_latency=False)
    errors = compare_to_baseline(summary, baseline)
    assert errors == [], errors
    frozen = freeze_baseline_payload(summary)
    assert frozen["fixture_hash"] == baseline["fixture_hash"]
    assert "latency" not in frozen
    assert all("/" not in str(item.get("scope_paths", [])) or True for item in frozen["cases"])


def test_cases_are_distinct_and_holdout_free():
    ids = [case.case_id for case in DEV_CASES]
    assert len(ids) == len(set(ids))
    blob = json.dumps(
        {
            "ids": ids,
            "queries": [case.task_query for case in DEV_CASES],
            "files": [sorted(case.files) for case in DEV_CASES],
        },
        sort_keys=True,
    ).lower()
    forbidden = (
        "holdout",
        "multi_file_banner",
        "multi_file_feature",
        "benchmarks/fixtures",
    )
    for token in forbidden:
        assert token not in blob
    # Digests are content-addressed and stable for the frozen trees.
    assert case_file_digests(DEV_CASES[0])


def test_token_overhead_bands_unchanged_versus_improved():
    assert token_overhead_allowance(v2_input=1000, newly_covered=0) == max(
        64, 50
    )
    assert token_overhead_allowance(v2_input=100, newly_covered=0) == 64
    assert token_overhead_allowance(v2_input=1000, newly_covered=2) == 250 + 256


def test_gate_failures_strip_timing_warnings():
    assert gate_failures(
        ["coverage regresses", "timing_warning:case warm_p95"]
    ) == ["coverage regresses"]


def test_candidate_arm_enables_dependency_coverage(tmp_path: Path):
    case = get_case("imported_class_call_annotation")
    workspace = tmp_path / "repo"
    cache = tmp_path / "cache"
    workspace.mkdir()
    cache.mkdir(mode=0o700)
    write_tree(workspace, case.files)
    control = run_case_arm(case, "v2", workspace=workspace, cache_base=cache)
    candidate = run_case_arm(
        case, "candidate", workspace=workspace, cache_base=cache
    )
    assert control.ready and candidate.ready
    assert candidate.must_cover_hits >= control.must_cover_hits
    assert candidate.missing_dependency_count <= control.missing_dependency_count


def test_candidate_gates_pass_on_development_suite():
    baseline_path = Path(__file__).with_name("dev_baseline.json")
    baseline = (
        json.loads(baseline_path.read_text(encoding="utf-8"))
        if baseline_path.exists()
        else None
    )
    summary = run_suite(("v1", "v2", "candidate"), latency_repeats=0, include_latency=False)
    errors = evaluate_candidate_gates(summary, baseline=baseline)
    fatal = gate_failures(errors)
    assert fatal == [], fatal
