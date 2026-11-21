from __future__ import annotations

from pathlib import Path

import pytest

from lean_weave.app import AppContext, create_repository_map
from lean_weave.code_index import CodeIndex
from lean_weave.config.models import AppConfig, ContextConfig
from lean_weave.context.builder import ContextBuilder
from lean_weave.context.models import ContextQuery, ExpansionLevel
from lean_weave.context.render import UNTRUSTED_END, UNTRUSTED_START
from lean_weave.context.repository import Repository
from lean_weave.errors import ForbiddenContext, InvalidContextRequest, RepositoryMapError
from lean_weave.repository_map import MapLimits, RepositoryMap


def _write(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def test_default_build_unchanged_without_indexed_map(tmp_path: Path, cache: Path):
    root = tmp_path / "repo"
    _write(root, {"a.py": "def a():\n    return 1\n"})
    repository = Repository(root, ContextConfig())
    query = ContextQuery(task="preview", level=ExpansionLevel.MAP, allow_git=False)
    first = ContextBuilder(repository).build(query)
    second = ContextBuilder(repository).build(query)
    assert first.messages == second.messages
    assert UNTRUSTED_START not in first.user_prompt


def test_indexed_preview_wraps_untrusted_and_scopes(tmp_path: Path, cache: Path):
    root = tmp_path / "repo"
    _write(
        root,
        {
            "pkg/core.py": "def helper():\n    return 1\n",
            "pkg/other.py": "def other():\n    return 1\n",
            "app.py": "from pkg.core import helper\n",
        },
    )
    index = CodeIndex(root, cache_base=cache)
    index.build()
    repository = Repository(root, ContextConfig())
    service = RepositoryMap(index, limits=MapLimits())
    query = ContextQuery(
        task="map preview",
        level=ExpansionLevel.MAP,
        paths=["pkg/core.py"],
        allow_git=False,
    )
    result = ContextBuilder(repository).build(query, indexed_map=service)
    assert result.pack.excerpts == []
    assert result.pack.symbols == []
    assert UNTRUSTED_START in result.user_prompt
    assert UNTRUSTED_END in result.user_prompt
    assert all("pkg/core.py" in path or path == "pkg/core.py" for path in result.pack.relevant_paths)
    assert any("pkg/core" in line for line in result.pack.repository_map)


def test_disjoint_scope_is_empty_set(tmp_path: Path, cache: Path):
    root = tmp_path / "repo"
    _write(root, {"a.py": "def a():\n    return 1\n", "b.py": "def b():\n    return 1\n"})
    index = CodeIndex(root, cache_base=cache)
    index.build()
    repository = Repository(root, ContextConfig())
    service = RepositoryMap(index, limits=MapLimits())
    query = ContextQuery(
        task="disjoint",
        level=ExpansionLevel.MAP,
        paths=["a.py"],
        scope_paths=["b.py"],
        allow_git=False,
    )
    result = ContextBuilder(repository).build(query, indexed_map=service)
    assert result.pack.repository_map == []
    assert result.pack.relevant_paths == []


def test_missing_scoped_path_is_not_found(tmp_path: Path, cache: Path):
    root = tmp_path / "repo"
    _write(root, {"a.py": "def a():\n    return 1\n"})
    index = CodeIndex(root, cache_base=cache)
    index.build()
    repository = Repository(root, ContextConfig())
    service = RepositoryMap(index, limits=MapLimits())
    query = ContextQuery(
        task="missing",
        level=ExpansionLevel.MAP,
        paths=["missing.py"],
        allow_git=False,
    )
    with pytest.raises(RepositoryMapError) as exc:
        ContextBuilder(repository).build(query, indexed_map=service)
    assert exc.value.code == "not_found"


def test_incompatible_modes_rejected(tmp_path: Path, cache: Path):
    root = tmp_path / "repo"
    _write(root, {"a.py": "def a():\n    return 1\n"})
    index = CodeIndex(root, cache_base=cache)
    index.build()
    repository = Repository(root, ContextConfig())
    service = RepositoryMap(index, limits=MapLimits())
    with pytest.raises(InvalidContextRequest):
        ContextBuilder(repository).build(
            ContextQuery(task="staged", level=ExpansionLevel.MAP, stage="plan"),
            indexed_map=service,
        )
    with pytest.raises(InvalidContextRequest):
        ContextBuilder(repository).build(
            ContextQuery(task="symbols", level=ExpansionLevel.SYMBOLS),
            indexed_map=service,
        )


def test_policy_mismatch_fails_before_index_io(tmp_path: Path, cache: Path, monkeypatch):
    root = tmp_path / "repo"
    _write(root, {"a.py": "def a():\n    return 1\n"})
    index = CodeIndex(root, cache_base=cache)
    index.build()
    service = RepositoryMap(index, limits=MapLimits())
    foreign = tmp_path / "other"
    foreign.mkdir()
    _write(foreign, {"a.py": "def a():\n    return 1\n"})
    foreign_repo = Repository(foreign, ContextConfig())

    def fail_read():
        raise AssertionError("must not open index on checkout mismatch")

    monkeypatch.setattr(service, "_load", fail_read)
    with pytest.raises(ForbiddenContext):
        ContextBuilder(foreign_repo).build(
            ContextQuery(task="foreign", level=ExpansionLevel.MAP),
            indexed_map=service,
        )

    loose = Repository(
        root,
        ContextConfig(exclusions=["*.bak"]),
    )
    with pytest.raises(ForbiddenContext):
        ContextBuilder(loose).build(
            ContextQuery(task="policy", level=ExpansionLevel.MAP),
            indexed_map=service,
        )


def test_create_repository_map_is_lazy():
    context = AppContext(config=AppConfig(), config_source=None)
    service = create_repository_map(context)
    assert isinstance(service, RepositoryMap)
