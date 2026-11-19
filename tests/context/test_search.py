import shutil
import subprocess
from pathlib import Path

import pytest

from lean_weave.config import ContextConfig
from lean_weave.context.repository import Repository
from lean_weave.context.search import search
from lean_weave.errors import InvalidContextRequest

import lean_weave.context.search as search_module

from .conftest import write_files


@pytest.fixture
def search_repo(project: Path):
    write_files(
        project,
        {
            "a.py": "alpha\nbeta",
            "b.py": "alpha\r\nAlpha\r\nalpha",
            "u.py": "café alpha\n",
            "dash.py": "-needle\n",
        },
    )
    return Repository(project)


def _force_python(monkeypatch):
    monkeypatch.setattr(search_module.shutil, "which", lambda name: None)


def _force_rg(monkeypatch):
    executable = shutil.which("rg")
    if executable is None:
        pytest.skip("ripgrep is not installed")
    monkeypatch.setattr(search_module.shutil, "which", lambda name: executable)
    return executable


def test_python_and_rg_are_equivalent(search_repo, monkeypatch):
    _force_python(monkeypatch)
    python_result = search(search_repo, "alpha")
    _force_rg(monkeypatch)
    rg_result = search(search_repo, "alpha")
    assert python_result.backend == "python"
    assert rg_result.backend == "rg"
    assert [(h.path, h.line, h.text) for h in python_result.hits] == [
        (h.path, h.line, h.text) for h in rg_result.hits
    ]


def test_crlf_unicode_and_no_final_newline(search_repo, monkeypatch):
    _force_python(monkeypatch)
    result = search(search_repo, "alpha")
    assert [(hit.path, hit.line) for hit in result.hits] == [
        ("a.py", 1),
        ("b.py", 1),
        ("b.py", 3),
        ("u.py", 1),
    ]
    assert all("\r" not in hit.text for hit in result.hits)
    assert search(search_repo, "café").hits[0].path == "u.py"


def test_leading_dash_query_and_no_matches(search_repo, monkeypatch):
    _force_python(monkeypatch)
    assert search(search_repo, "-needle").hits[0].path == "dash.py"
    assert search(search_repo, "absent").hits == []


def test_missing_rg_falls_back(search_repo, monkeypatch):
    _force_python(monkeypatch)
    result = search(search_repo, "alpha")
    assert result.backend == "python"
    assert result.degraded_reason == "rg_missing"


def test_rg_never_receives_repository_paths(search_repo, monkeypatch):
    _force_rg(monkeypatch)
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["input"] = kwargs["input"]
        return subprocess.CompletedProcess(argv, 1, b"", b"")

    monkeypatch.setattr(search_module.subprocess, "run", fake_run)
    search(search_repo, "alpha")
    argv = captured["argv"]
    assert "--" in argv
    assert argv[-1] == "alpha"
    assert not any(str(search_repo.root) in item for item in argv)
    assert b"alpha\n" in captured["input"]


@pytest.mark.parametrize(
    "behavior,reason",
    [
        ("timeout", "rg_error"),
        ("overflow", "rg_overflow"),
        ("malformed", "rg_malformed"),
    ],
)
def test_rg_failures_discard_partial_results(search_repo, monkeypatch, behavior, reason):
    _force_rg(monkeypatch)

    def fake_run(argv, **kwargs):
        if behavior == "timeout":
            raise subprocess.TimeoutExpired(argv, 1)
        if behavior == "overflow":
            return subprocess.CompletedProcess(argv, 0, b"x" * (search_module.RG_OUTPUT_LIMIT + 1), b"")
        return subprocess.CompletedProcess(argv, 0, b"not-json\n", b"")

    monkeypatch.setattr(search_module.subprocess, "run", fake_run)
    result = search(search_repo, "alpha")
    assert result.backend == "python"
    assert result.degraded_reason == reason
    assert result.hits


def test_rg_exit_one_means_no_matches(search_repo, monkeypatch):
    _force_rg(monkeypatch)
    monkeypatch.setattr(
        search_module.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, b"", b""),
    )
    result = search(search_repo, "alpha")
    assert result.backend == "rg"
    assert result.hits == []


def test_limit_truncates(search_repo, monkeypatch):
    _force_python(monkeypatch)
    result = search(search_repo, "alpha", limit=1)
    assert len(result.hits) == 1
    assert result.truncated is True


def test_path_filter_applies_before_limit(search_repo, monkeypatch):
    _force_python(monkeypatch)
    result = search(search_repo, "alpha", limit=1, paths=["u.py"])
    assert [(hit.path, hit.line) for hit in result.hits] == [("u.py", 1)]
    assert result.truncated is False


@pytest.mark.parametrize("query", ["", "a\nb", "a\x00b"])
def test_invalid_query(search_repo, query):
    with pytest.raises(InvalidContextRequest):
        search(search_repo, query)


def test_limit_cannot_exceed_configuration(search_repo):
    with pytest.raises(InvalidContextRequest):
        search(search_repo, "alpha", limit=ContextConfig().search_hits_max + 1)
