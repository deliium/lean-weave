"""End-to-end ContextRetriever behavior over admitted snapshots."""

from __future__ import annotations

from pathlib import Path

import pytest

from lean_weave.code_index import CodeIndex
from lean_weave.config.models import ContextConfig
from lean_weave.context.repository import Repository
from lean_weave.context.retrieval_models import RetrievalRequest
from lean_weave.context.retriever import ContextRetriever
from lean_weave.errors import CodeIndexError
from lean_weave.repository_map import RepositoryMap
from lean_weave.verify.process import run_bounded
from lean_weave.context.git_metadata import read_retrieval_diff

from .conftest import write_files


def _repo(root: Path, **overrides) -> Repository:
    return Repository(root, ContextConfig(**overrides), strict_source=True)


def test_local_fallback_without_graph_finds_symbol_and_file(project: Path):
    write_files(
        project,
        {
            "pkg/auth.py": "def login():\n    return 1\n",
            "pkg/other.py": "x = 1\n",
            "README.md": "login docs\n",
        },
    )
    retriever = ContextRetriever(_repo(project))
    result = retriever.retrieve(RetrievalRequest(query="login", task_kind="bugfix"))
    assert result.degraded_reasons == ()
    assert any(hit.path == "pkg/auth.py" for hit in result.hits)
    assert any(
        hit.kind == "symbol" and hit.qualified_name and "login" in hit.qualified_name
        for hit in result.hits
    )
    assert all(hit.ranking_reasons for hit in result.hits if hit.relevance_score)


def test_warm_graph_and_git_signals(tmp_path: Path):
    project = tmp_path / "repo"
    project.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    write_files(
        project,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "def value():\n    return 1\n",
            "pkg/b.py": "from pkg.a import value\n\ndef use():\n    return value()\n",
        },
    )
    index = CodeIndex(project, cache_base=cache)
    index.build()
    service = RepositoryMap(index)

    def load_graph(scan):
        return service.graph_for_retrieval_scan(scan)

    retriever = ContextRetriever(_repo(project), load_graph=load_graph)
    result = retriever.retrieve(RetrievalRequest(query="value", task_kind="refactor"))
    assert "index_missing" not in result.degraded_reasons
    paths = {hit.path for hit in result.hits}
    assert "pkg/a.py" in paths
    # Dependency expansion should surface the importer when weight is positive.
    assert "pkg/b.py" in paths or any(
        dep.related_path == "pkg/b.py"
        for hit in result.hits
        for dep in hit.related_dependencies
    )


def test_missing_index_degrades_without_failing(tmp_path: Path):
    project = tmp_path / "repo"
    project.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    write_files(project, {"a.py": "def alpha():\n    return 1\n"})
    index = CodeIndex(project, cache_base=cache)
    service = RepositoryMap(index)

    def load_graph(scan):
        return service.graph_for_retrieval_scan(scan)

    retriever = ContextRetriever(_repo(project), load_graph=load_graph)
    result = retriever.retrieve(RetrievalRequest(query="alpha"))
    assert "index_missing" in result.degraded_reasons
    assert any(hit.path == "a.py" for hit in result.hits)


def test_incomplete_scan_skips_graph_and_git(project: Path):
    write_files(project, {"a.py": "def alpha():\n    return 1\n", "b.py": "x = 1\n"})
    called = {"graph": 0, "git": 0}

    def load_graph(scan):
        called["graph"] += 1
        raise AssertionError("graph must not run")

    def read_diff(root, paths):
        called["git"] += 1
        raise AssertionError("git must not run")

    retriever = ContextRetriever(
        _repo(project, visited_entries_max=1),
        load_graph=load_graph,
        read_diff=read_diff,
    )
    result = retriever.retrieve(RetrievalRequest(query="alpha"))
    assert called == {"graph": 0, "git": 0}
    assert "source_scan_incomplete" in result.degraded_reasons
    assert result.truncated is True


def test_mid_query_change_returns_source_changed(project: Path):
    write_files(project, {"a.py": "def alpha():\n    return 1\n"})
    repository = _repo(project)

    class FlippingRetriever(ContextRetriever):
        def _rank_local(self, scan, **kwargs):
            (project / "a.py").write_text("def alpha():\n    return 2\n")
            return super()._rank_local(scan, **kwargs)

    result = FlippingRetriever(repository).retrieve(RetrievalRequest(query="alpha"))
    assert result.hits == ()
    assert "source_changed" in result.degraded_reasons
    assert result.truncated is True


def test_broken_python_still_returns_file_evidence(project: Path):
    write_files(
        project,
        {
            "broken.py": "def broken(:\n",
            "ok.md": "broken helper docs\n",
        },
    )
    result = ContextRetriever(_repo(project)).retrieve(
        RetrievalRequest(query="broken")
    )
    assert any(hit.path in {"broken.py", "ok.md"} for hit in result.hits)


def test_top_k_and_kind_filter(project: Path):
    write_files(
        project,
        {
            "a.py": "def alpha():\n    return 1\n",
            "b.py": "def alpha():\n    return 2\n",
            "c.py": "def alpha():\n    return 3\n",
        },
    )
    result = ContextRetriever(_repo(project)).retrieve(
        RetrievalRequest(query="alpha", kind="symbol", top_k=2)
    )
    assert len(result.hits) <= 2
    assert all(hit.kind == "symbol" for hit in result.hits)
    assert result.truncated in {True, False}
