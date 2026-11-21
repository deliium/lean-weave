"""Batched snapshot literal search and query preparation tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lean_weave.context.models import iter_lines
from lean_weave.context.retrieval_models import RetrievalRequest
from lean_weave.context.retrieval_query import prepare_query
from lean_weave.context.search import search_snapshot_literals

import lean_weave.context.search as search_module


def _force_python(monkeypatch):
    monkeypatch.setattr(search_module.shutil, "which", lambda name: None)


def _force_rg(monkeypatch):
    executable = __import__("shutil").which("rg")
    if executable is None:
        pytest.skip("ripgrep is not installed")
    monkeypatch.setattr(search_module.shutil, "which", lambda name: executable)
    return executable


def test_prepare_query_phrase_alias_and_expansion():
    prepared = prepare_query(
        RetrievalRequest(query="fix LoginBug in src/auth/session.py")
    )
    assert prepared.phrase == "fix LoginBug in src/auth/session.py"
    assert "LoginBug" in prepared.selectors
    assert "src/auth/session.py" in prepared.selectors
    assert "fix" not in prepared.primary
    assert "in" not in prepared.primary
    assert "Login" in prepared.expanded or "Bug" in prepared.expanded
    assert "auth" in prepared.expanded
    assert "session" in prepared.expanded


def test_prepare_query_dedupes_and_caps():
    tokens = " ".join(f"token{index}" for index in range(20))
    prepared = prepare_query(RetrievalRequest(query=tokens))
    assert len(prepared.selectors) == 12
    assert prepared.primary_capped is True
    prepared_dup = prepare_query(RetrievalRequest(query="Alpha alpha ALPHA"))
    assert prepared_dup.primary == ("Alpha",)


def test_prepare_query_keeps_dotted_selector():
    prepared = prepare_query(RetrievalRequest(query="Call SymbolRef.load now"))
    assert "SymbolRef.load" in prepared.selectors


def test_snapshot_backends_agree_on_masks(monkeypatch):
    lines = {
        "a.py": ["alpha beta", "other"],
        "b.py": ["Alpha", "alpha"],
    }
    phrase = "alpha"
    primary = ("alpha",)
    expanded = ("beta",)

    _force_python(monkeypatch)
    python_result = search_snapshot_literals(lines, phrase, primary, expanded)
    _force_rg(monkeypatch)
    rg_result = search_snapshot_literals(lines, phrase, primary, expanded)

    assert python_result.backend == "python"
    assert rg_result.backend == "rg"
    assert [
        (f.path, f.primary_mask, f.expanded_mask, f.phrase, [a.line for a in f.anchors])
        for f in python_result.files
    ] == [
        (f.path, f.primary_mask, f.expanded_mask, f.phrase, [a.line for a in f.anchors])
        for f in rg_result.files
    ]


def test_crlf_mapping_uses_canonical_lines(monkeypatch):
    _force_python(monkeypatch)
    text = "alpha\r\nbeta\r\n"
    lines = {"a.py": iter_lines(text)}
    result = search_snapshot_literals(lines, "alpha", ("alpha",), ())
    assert result.files[0].anchors[0].line == 1
    assert result.match_line_count == 1


def test_unicode_and_leading_dash_literals(monkeypatch):
    _force_python(monkeypatch)
    lines = {"u.py": ["café -needle"], "r.py": ["a.*b"]}
    cafe = search_snapshot_literals(lines, "café", ("café",), ())
    assert cafe.files[0].path == "u.py"
    dash = search_snapshot_literals(lines, "-needle", ("-needle",), ())
    assert dash.files[0].path == "u.py"
    meta = search_snapshot_literals(lines, "a.*b", ("a.*b",), ())
    assert meta.files[0].path == "r.py"


def test_large_corpus_bypasses_rg(monkeypatch):
    called = {"rg": False}

    def fake_which(name):
        return "/usr/bin/rg"

    def fake_run(*args, **kwargs):
        called["rg"] = True
        raise AssertionError("rg must not run for large input")

    monkeypatch.setattr(search_module.shutil, "which", fake_which)
    monkeypatch.setattr(search_module.subprocess, "run", fake_run)
    line = "x" * 100
    lines = {f"f{index}.py": [line, "needle"] for index in range(700)}
    result = search_snapshot_literals(lines, "needle", ("needle",), ())
    assert called["rg"] is False
    assert result.backend == "python"
    assert result.degraded_reason is None
    assert result.match_line_count == 700


def test_file_anchors_capped_but_masks_cover_all_lines(monkeypatch):
    _force_python(monkeypatch)
    lines = {
        "a.py": [f"needle line-{index}" for index in range(1, 12)],
    }
    callbacks: list[tuple[str, int]] = []

    def on_match(path, line, primary_mask, expanded_mask, phrase):
        callbacks.append((path, line))

    result = search_snapshot_literals(
        lines,
        "needle",
        ("needle",),
        (),
        on_match=on_match,
    )
    assert result.anchors_capped is True
    assert len(result.files[0].anchors) == 8
    assert result.files[0].primary_mask == 1
    assert result.files[0].phrase is True
    assert len(callbacks) == 11
    assert callbacks == [("a.py", index) for index in range(1, 12)]


def test_malformed_rg_discards_partial_before_python(monkeypatch):
    _force_rg(monkeypatch)
    lines = {"a.py": ["alpha", "beta"]}
    callbacks: list[tuple[str, int]] = []

    def on_match(path, line, primary_mask, expanded_mask, phrase):
        callbacks.append((path, line))

    def fake_run(argv, **kwargs):
        # One plausible row, then garbage — must not callback before fallback.
        stdout = b"1:alpha\nnot-a-hit\n"
        return subprocess.CompletedProcess(argv, 0, stdout, b"")

    monkeypatch.setattr(search_module.subprocess, "run", fake_run)
    result = search_snapshot_literals(
        lines,
        "alpha",
        ("alpha",),
        (),
        on_match=on_match,
    )
    assert result.backend == "python"
    assert result.degraded_reason == "rg_malformed"
    assert callbacks == [("a.py", 1)]


def test_empty_input_skips_subprocess(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("subprocess must not run")

    monkeypatch.setattr(search_module.subprocess, "run", boom)
    result = search_snapshot_literals({}, "alpha", ("alpha",), ())
    assert result.files == ()
    assert result.line_count == 0
