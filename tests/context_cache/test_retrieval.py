from __future__ import annotations

from pathlib import Path

import pytest

from lean_weave.config.models import ContextCacheConfig, ContextConfig
from lean_weave.context.repository import Repository
from lean_weave.context.retrieval_models import RetrievalRequest
from lean_weave.context.retriever import ContextRetriever
from lean_weave.context_cache import ArtifactKind
from lean_weave.context_cache.service import SqliteContextCache


@pytest.fixture
def project(tmp_path: Path, monkeypatch):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "alpha.py").write_text(
        "def alpha_handler(value):\n    return value\n", encoding="utf-8"
    )
    (root / "src" / "beta.py").write_text(
        "from src.alpha import alpha_handler\n\ndef beta():\n    return alpha_handler(1)\n",
        encoding="utf-8",
    )
    (root / "src" / "gamma.py").write_text(
        "def gamma():\n    return 3\n", encoding="utf-8"
    )
    base = tmp_path / "cache"
    base.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CACHE_HOME", str(base))
    repository = Repository(root, ContextConfig(), strict_source=True)
    cache = SqliteContextCache(
        root,
        config=ContextCacheConfig(enabled=True),
        context_config=ContextConfig(),
        cache_base=base,
    )
    return root, repository, cache


def _retriever(repository, cache):
    return ContextRetriever(repository, cache=cache)


def test_warm_retrieval_skips_ranking(project, monkeypatch):
    root, repository, cache = project
    request = RetrievalRequest(query="alpha handler", task_kind="feature")
    cold = _retriever(repository, cache).retrieve(request)
    calls = {"n": 0}
    original = ContextRetriever._rank_local

    def counting(self, *args, **kwargs):
        calls["n"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ContextRetriever, "_rank_local", counting)
    warm_repository = Repository(root, ContextConfig(), strict_source=True)
    warm = _retriever(warm_repository, cache).retrieve(request)
    assert warm == cold
    assert calls["n"] == 0


def test_query_and_scope_change_the_key(project):
    root, repository, cache = project
    first = _retriever(repository, cache).retrieve(RetrievalRequest(query="alpha"))
    second = _retriever(repository, cache).retrieve(RetrievalRequest(query="gamma"))
    assert first != second

    scoped = _retriever(repository, cache).rank_checked(
        RetrievalRequest(query="alpha"), allowed_paths=frozenset({"src/alpha.py"})
    )[0]
    everything = _retriever(repository, cache).retrieve(RetrievalRequest(query="alpha"))
    assert scoped != everything


def test_changed_scope_reuses_path_local_candidates(project):
    root, repository, cache = project
    request = RetrievalRequest(query="alpha handler")
    _retriever(repository, cache).rank_checked(request)
    _retriever(repository, cache).rank_checked(
        request, allowed_paths=frozenset({"src/alpha.py"})
    )
    assert cache.stats().count("hit", ArtifactKind.RETRIEVAL_CANDIDATES) > 0


def test_source_edit_changes_result(project):
    root, repository, cache = project
    request = RetrievalRequest(query="alpha handler")
    first = _retriever(repository, cache).retrieve(request)
    (root / "src" / "beta.py").write_text(
        "from src.alpha import alpha_handler\n\ndef renamed():\n    return alpha_handler(2)\n",
        encoding="utf-8",
    )
    fresh = Repository(root, ContextConfig(), strict_source=True)
    second = _retriever(fresh, cache).retrieve(request)
    uncached = _retriever(fresh, None).retrieve(request)
    assert second == uncached
    assert second != first
