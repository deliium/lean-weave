from __future__ import annotations

from pathlib import Path

import pytest

from lean_weave.code_index import CodeIndex
from lean_weave.config.models import ContextCacheConfig, ContextConfig
from lean_weave.context_cache.imports import ImportCache
from lean_weave.context_cache.service import CachePolicy, SqliteContextCache
from lean_weave.repository_map.graph import refresh_graph


@pytest.fixture
def project(tmp_path: Path, monkeypatch):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "b.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "pkg" / "a.py").write_text(
        "from pkg.b import VALUE\n\ndef use():\n    return VALUE\n", encoding="utf-8"
    )
    (root / "pkg" / "c.py").write_text("def c():\n    return 3\n", encoding="utf-8")
    base = tmp_path / "cache"
    base.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CACHE_HOME", str(base))
    index = CodeIndex(root, context_config=ContextConfig(), cache_base=base)
    cache = SqliteContextCache(
        root,
        config=ContextCacheConfig(enabled=True),
        context_config=ContextConfig(),
        cache_base=base,
    )
    return root, index, cache


def _resolver(cache, index):
    generation = index.read_generation()
    policy = CachePolicy.from_context(
        index.context_config, state_dir=index.state_dir, protected_paths=index.protected_paths
    )
    imports = ImportCache(cache, policy=policy).bind(generation.header)
    batch = cache.begin_batch()

    def resolve(node, record, catalogs):
        return imports.resolve(record, node, catalogs, batch=batch)

    return generation, resolve, batch


def test_warm_graph_reuses_importer_relations(project, monkeypatch):
    root, index, cache = project
    index.build()
    generation, resolver, batch = _resolver(cache, index)
    first = refresh_graph(None, generation, resolver=resolver)
    cache.flush(batch)

    import lean_weave.context_cache.imports as imports_module
    calls = {"n": 0}
    original = imports_module.resolve_imports_for

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(imports_module, "resolve_imports_for", counting)
    generation2, resolver2, batch2 = _resolver(cache, index)
    second = refresh_graph(None, generation2, resolver=resolver2)
    cache.flush(batch2)
    assert calls["n"] == 0
    assert first.relations == second.relations


def test_dependency_edit_recomputes_only_affected_importers(project, monkeypatch):
    root, index, cache = project
    index.build()
    generation, resolver, batch = _resolver(cache, index)
    previous = refresh_graph(None, generation, resolver=resolver)
    cache.flush(batch)

    (root / "pkg" / "b.py").write_text("VALUE = 2\n", encoding="utf-8")
    index.update()
    generation2, resolver2, batch2 = _resolver(cache, index)

    import lean_weave.context_cache.imports as imports_module
    calls = {"n": 0}
    original = imports_module.resolve_imports_for

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(imports_module, "resolve_imports_for", counting)
    graph = refresh_graph(previous, generation2, resolver=resolver2)
    cache.flush(batch2)
    # b.py changed and a.py imports it; unrelated c.py remains a hit.
    assert calls["n"] == 2
    assert any(relation.spelling == "pkg.b.VALUE" for relation in graph.relations)
