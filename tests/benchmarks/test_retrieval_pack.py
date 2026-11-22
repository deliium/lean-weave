from __future__ import annotations

import pytest

from lean_weave.benchmark.retrieval import evaluate_retrieval, load_cases


def test_real_multi_file_labels_separate_ranking_from_pack():
    case = next(item for item in load_cases() if item.id == "multi_file_banner")
    assert case.relevant_ranges
    assert case.required_dependencies
    v1 = evaluate_retrieval(case, "v1_default")
    v2 = evaluate_retrieval(case, "v2_default")
    assert v1["ranking"]["files"]["P@1"] == 1.0
    assert v2["ranking"]["files"]["P@1"] == 1.0
    assert v1["pack"]["selected_source_tokens"] > 0
    assert v2["pack"]["selected_source_tokens"] > 0
    assert v1["pack"]["missing_dependency_count"] == 1
    assert v2["pack"]["missing_dependency_count"] == 0
    assert v1["provenance"] == v2["provenance"] == "local_estimate"
    assert "text" not in str(v1)


def test_symbol_metrics_null_when_no_symbol_labels():
    case = next(item for item in load_cases() if item.id == "dev_auth_refactor_importer")
    assert case.symbols == ()
    payload = evaluate_retrieval(case, "v2_default")
    assert payload["ranking"]["symbols"]["applicable"] is False
    assert "P@1" not in payload["ranking"]["symbols"]


def test_tight_pack_can_miss_required_dependency():
    payload = evaluate_retrieval("holdout_invoice_tight", "v2_tight")
    assert payload["pack"]["required_dependency_count"] >= 1
    assert payload["pack"]["missing_dependency_count"] >= 0
    assert payload["conditions"]["source_tokens"] == 512


def test_cache_disabled_is_distinct_from_warm_and_cold():
    cold = evaluate_retrieval("dev_auth_login", "v2_cache_cold")
    warm = evaluate_retrieval("dev_auth_login", "v2_cache_warm")
    disabled = evaluate_retrieval("dev_auth_login", "v2_cache_disabled")
    assert cold["cache"]["enabled"] is True
    assert warm["cache"]["enabled"] is True
    assert disabled["cache"]["enabled"] is False
    assert disabled["cache"]["available"] is False
    assert warm["cache"]["available"] is True


def test_overlapping_labels_are_unioned():
    payload = evaluate_retrieval("dev_auth_login_overlap", "v2_default")
    assert payload["pack"]["available"] is True
    assert payload["pack"]["relevant_token_ratio"] is not None
