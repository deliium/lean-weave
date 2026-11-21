"""Unit goldens for hybrid retrieval ranking helpers."""

from __future__ import annotations

from lean_weave.config.models import RetrievalWeights
from lean_weave.context.retrieval_ranking import (
    exact_symbol_score,
    effective_weights,
    lexical_score,
    live_symbol_id,
    name_score_for_file,
    relevance_score,
    ranking_reasons,
    select_file_window,
    estimate_window_cost,
)


def test_live_symbol_id_is_stable():
    first = live_symbol_id("a.py", "helper", "function", 0)
    second = live_symbol_id("a.py", "helper", "function", 0)
    assert first == second
    assert first.startswith("live:")
    assert first != live_symbol_id("a.py", "helper", "function", 1)


def test_exact_and_name_precedence():
    assert exact_symbol_score("pkg.helper", "helper", ["pkg.helper"]) == 1000
    assert exact_symbol_score("pkg.helper", "helper", ["helper"]) == 850
    assert exact_symbol_score("helper", "helper", ["helper"]) == 1000
    assert exact_symbol_score("pkg.Widget.render", "render", ["Widget.render"]) == 850
    assert name_score_for_file("pkg/auth.py", ("pkg/auth.py",), module_name="pkg.auth") == 1000
    assert name_score_for_file("pkg/auth.py", ("auth",), module_name="pkg.auth") == 750


def test_lexical_and_score_golden():
    assert lexical_score(
        phrase=True, primary_mask=0, expanded_mask=0, primary_count=1, expanded_count=1
    ) == 1000
    score = lexical_score(
        phrase=False,
        primary_mask=0b1,
        expanded_mask=0b1,
        primary_count=2,
        expanded_count=2,
    )
    assert score == min(950, 800 * 1 // 2 + 150 * 1 // 2)
    weights = effective_weights(RetrievalWeights(), "feature")
    signals = {
        "exact_symbol": 0,
        "name": 750,
        "lexical": 1000,
        "import_dependency": 0,
        "reverse_dependency": 0,
        "git_diff": 0,
        "related_test": 0,
        "map_importance": 0,
    }
    expected = (750 * 24 + 1000 * 24) // sum(weights.values())
    assert relevance_score(signals, weights) == expected
    reasons = ranking_reasons(signals, weights)
    assert [item.signal for item in reasons] == ["name", "lexical"]


def test_bugfix_doubles_lexical_weight():
    feature = effective_weights(RetrievalWeights(), "feature")
    bugfix = effective_weights(RetrievalWeights(), "bugfix")
    assert bugfix["lexical"] == feature["lexical"] * 2
    assert bugfix["git_diff"] == feature["git_diff"] * 2


def test_empty_and_window_cost():
    assert estimate_window_cost("", empty=True) == 0
    start, end, kind, text = select_file_window(
        ["a", "b", "c", "d", "e", "f"],
        empty=False,
        lexical_positive=False,
        anchor=None,
        char_cap=12000,
    )
    assert (start, end, kind) == (1, 5, "file_head")
    assert text == "a\nb\nc\nd\ne"
