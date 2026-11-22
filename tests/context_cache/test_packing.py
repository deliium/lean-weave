from __future__ import annotations

from pathlib import Path

import pytest

from lean_weave.config.models import ContextCacheConfig, ContextConfig
from lean_weave.context.packer import ContextPacker
from lean_weave.context.packing_models import PackRequest
from lean_weave.context.repository import Repository
from lean_weave.context.retriever import ContextRetriever
from lean_weave.context import packing_source
from lean_weave.context_cache.service import SqliteContextCache


@pytest.fixture
def project(tmp_path: Path, monkeypatch):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "alpha.py").write_text(
        "def alpha_handler(value):\n    return value + 1\n\n"
        "class Handler:\n    def run(self):\n        return alpha_handler(2)\n",
        encoding="utf-8",
    )
    (root / "src" / "beta.py").write_text(
        "from src.alpha import alpha_handler\n\ndef beta():\n    return alpha_handler(3)\n",
        encoding="utf-8",
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


def _packer(repository, cache):
    return ContextPacker(
        repository,
        retriever=ContextRetriever(repository, cache=cache),
        cache=cache,
    )


def _request() -> PackRequest:
    return PackRequest(
        task_id="task-1",
        query="alpha handler",
        task_specification="Change the alpha handler.",
        system_instructions="You are a careful engineer.",
        level="l1",
    )


def test_warm_pack_reuses_spans_and_keeps_output(project, monkeypatch):
    root, repository, cache = project
    cold = _packer(repository, cache).build(_request())
    assert cold.status == "ready"

    calls = {"n": 0}
    original = packing_source._python_spans

    def counting(snapshot):
        calls["n"] += 1
        return original(snapshot)

    monkeypatch.setattr(packing_source, "_python_spans", counting)
    fresh = Repository(root, ContextConfig(), strict_source=True)
    warm = _packer(fresh, cache).build(_request())
    assert warm.status == "ready"
    assert warm.pack.model_dump() == cold.pack.model_dump()
    assert calls["n"] == 0


def test_budget_change_still_evaluates_live(project):
    root, repository, cache = project
    request = PackRequest(
        task_id="task-2",
        query="alpha handler",
        task_specification="Change the alpha handler.",
        system_instructions="You are a careful engineer.",
        level="l0",
    )
    first = _packer(repository, cache).build(request)
    fresh = Repository(root, ContextConfig(), strict_source=True)
    second = _packer(fresh, cache).build(request)
    assert first.pack.messages == second.pack.messages
    assert first.pack.metrics.total_tokens == second.pack.metrics.total_tokens


def test_one_file_edit_keeps_unrelated_spans(project, monkeypatch):
    root, repository, cache = project
    _packer(repository, cache).build(_request())
    (root / "src" / "beta.py").write_text(
        "from src.alpha import alpha_handler\n\ndef changed():\n    return alpha_handler(9)\n",
        encoding="utf-8",
    )
    calls = {"n": 0}
    original = packing_source._python_spans

    def counting(snapshot):
        calls["n"] += 1
        return original(snapshot)

    monkeypatch.setattr(packing_source, "_python_spans", counting)
    fresh = Repository(root, ContextConfig(), strict_source=True)
    result = _packer(fresh, cache).build(_request())
    assert result.status == "ready"
    assert calls["n"] <= 1
