from __future__ import annotations

from pathlib import Path

import pytest

from lean_weave.code_index import CodeIndex
from lean_weave.config.models import ContextCacheConfig, ContextConfig
from lean_weave.context_cache.service import SqliteContextCache
from lean_weave.repository_map import MapLimits, RepositoryMap
from lean_weave.repository_map import render


@pytest.fixture
def project(tmp_path: Path, monkeypatch):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "a.py").write_text(
        "def alpha(value):\n    return value\n\nclass One:\n    def method(self):\n        return 1\n",
        encoding="utf-8",
    )
    (root / "pkg" / "b.py").write_text(
        "from pkg.a import alpha\n\ndef beta():\n    return alpha(2)\n",
        encoding="utf-8",
    )
    base = tmp_path / "cache"
    base.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CACHE_HOME", str(base))
    index = CodeIndex(root, context_config=ContextConfig(), cache_base=base)
    index.build()
    cache = SqliteContextCache(
        root,
        config=ContextCacheConfig(enabled=True),
        context_config=ContextConfig(),
        cache_base=base,
    )
    return root, index, cache


def test_warm_map_reuses_headers_and_definitions(project, monkeypatch):
    root, index, cache = project
    calls = {"header": 0, "definition": 0}
    original_header = render.module_row
    original_definition = render.definition_row

    def header(*args, **kwargs):
        calls["header"] += 1
        return original_header(*args, **kwargs)

    def definition(*args, **kwargs):
        calls["definition"] += 1
        return original_definition(*args, **kwargs)

    monkeypatch.setattr(render, "module_row", header)
    monkeypatch.setattr(render, "definition_row", definition)

    cold = RepositoryMap(index, cache=cache).show()
    cold_calls = dict(calls)
    calls.update(header=0, definition=0)
    warm = RepositoryMap(index, cache=cache).show()
    assert warm.text == cold.text
    assert warm.rows == cold.rows
    assert calls["header"] == 0
    assert calls["definition"] == 0
    assert cold_calls["header"] > 0


def test_changed_module_keeps_unrelated_rows(project, monkeypatch):
    root, index, cache = project
    first = RepositoryMap(index, cache=cache).show()
    (root / "pkg" / "b.py").write_text(
        "from pkg.a import alpha\n\ndef gamma():\n    return alpha(3)\n",
        encoding="utf-8",
    )
    index.update()
    calls = {"header": 0, "definition": 0}
    original_header = render.module_row
    original_definition = render.definition_row

    def header(*args, **kwargs):
        calls["header"] += 1
        return original_header(*args, **kwargs)

    def definition(*args, **kwargs):
        calls["definition"] += 1
        return original_definition(*args, **kwargs)

    monkeypatch.setattr(render, "module_row", header)
    monkeypatch.setattr(render, "definition_row", definition)
    second = RepositoryMap(index, cache=cache).show()
    assert second.text != first.text
    # a.py header and definitions are unchanged and must not be re-rendered.
    assert calls["header"] <= 1
    assert calls["definition"] <= 2


def test_budget_changes_reuse_rows(project):
    root, index, cache = project
    full = RepositoryMap(index, cache=cache).show()
    limited = RepositoryMap(
        index,
        cache=cache,
        limits=MapLimits(tokens_max=120, rows_max=4),
    ).show()
    assert limited.estimated_tokens <= 120
    assert limited.rows != full.rows or limited.truncated
