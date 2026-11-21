from __future__ import annotations

from pathlib import Path

import pytest

import lean_weave.code_index.index as index_module
import lean_weave.repository_map.graph as graph_module
from lean_weave.code_index import CodeIndex
from lean_weave.repository_map import build_graph, refresh_graph
from lean_weave.repository_map.models import GRAPH_CONTRACT_VERSION


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _by_path(graph, path: str):
    return next(node for node in graph.nodes if node.path == path)


def _relation(graph, importer: str, spelling: str):
    node = _by_path(graph, importer)
    matches = [
        item
        for item in graph.relations
        if item.importer_id == node.file_id and item.spelling == spelling
    ]
    assert len(matches) == 1
    return matches[0]


def _graph_signature(graph):
    return (
        graph.revision,
        graph.manifest_sha256,
        tuple(
            (
                node.path,
                node.file_id,
                node.module_name,
                node.digest,
                node.parse_status,
            )
            for node in graph.nodes
        ),
        tuple(
            (
                item.importer_id,
                item.statement_ordinal,
                item.name_ordinal,
                item.spelling,
                item.alias,
                item.status,
                item.reason,
                item.target_id,
                item.symbol_id,
                tuple(
                    (candidate.file_id, candidate.path, candidate.module_name)
                    for candidate in item.candidates
                ),
                item.source.path,
                item.source.start_line,
                item.source.end_line,
            )
            for item in graph.relations
        ),
        tuple(sorted((key, value) for key, value in graph.forward.items())),
        tuple(sorted((key, value) for key, value in graph.reverse.items())),
    )


def test_unchanged_refresh_is_hit(tmp_path: Path, cache: Path, monkeypatch):
    root = tmp_path / "repo"
    _write_tree(
        root,
        {
            "a.py": "import b\n",
            "b.py": "def run():\n    return 1\n",
        },
    )
    index = CodeIndex(root, cache_base=cache)
    index.build()
    generation = index.read_generation()
    first = build_graph(generation)

    def fail(*_args, **_kwargs):
        raise AssertionError("unchanged refresh must not resolve imports")

    monkeypatch.setattr(graph_module, "_resolve_imports_for", fail)
    second = refresh_graph(first, index.read_generation())
    assert second is first


def test_target_only_transitions_match_rebuild(tmp_path: Path, cache: Path, monkeypatch):
    root = tmp_path / "repo"
    _write_tree(
        root,
        {
            "pkg/child.py": "def child():\n    return 1\n",
            "importer.py": "import pkg.child\n",
            "other.py": "import json\n",
        },
    )
    index = CodeIndex(root, cache_base=cache)
    index.build()
    graph = build_graph(index.read_generation())
    assert _relation(graph, "importer.py", "pkg.child").status == "resolved"
    other_id = _by_path(graph, "other.py").file_id
    resolved_other = [
        item for item in graph.relations if item.importer_id == other_id
    ]

    parse_calls: list[str] = []
    real_extract = index_module.extract_python_facts

    def tracking(snapshot):
        parse_calls.append(snapshot.path)
        return real_extract(snapshot)

    monkeypatch.setattr(index_module, "extract_python_facts", tracking)

    # namespace → blocked by ordinary pkg.py
    (root / "pkg.py").write_text("def name():\n    return 1\n", encoding="utf-8")
    update = index.update()
    assert "importer.py" not in parse_calls
    assert update.count("parsed") == 1
    graph = refresh_graph(graph, index.read_generation())
    assert _graph_signature(graph) == _graph_signature(build_graph(index.read_generation()))
    blocked = _relation(graph, "importer.py", "pkg.child")
    assert blocked.status == "unresolved" and blocked.reason == "non_package_parent"
    assert graph.reverse[_by_path(graph, "pkg/child.py").file_id] == ()
    assert [
        item for item in graph.relations if item.importer_id == _by_path(graph, "other.py").file_id
    ] == resolved_other

    # blocked → ambiguous after pkg.pyi appears
    parse_calls.clear()
    (root / "pkg.pyi").write_text("def name() -> int: ...\n", encoding="utf-8")
    index.update()
    assert "importer.py" not in parse_calls
    graph = refresh_graph(graph, index.read_generation())
    assert _graph_signature(graph) == _graph_signature(build_graph(index.read_generation()))
    assert _relation(graph, "importer.py", "pkg.child").status == "ambiguous"

    # ambiguous → package after removing pkg.py and adding __init__.py
    parse_calls.clear()
    (root / "pkg.py").unlink()
    (root / "pkg.pyi").unlink()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    index.update()
    assert "importer.py" not in parse_calls
    graph = refresh_graph(graph, index.read_generation())
    assert _graph_signature(graph) == _graph_signature(build_graph(index.read_generation()))
    restored = _relation(graph, "importer.py", "pkg.child")
    assert restored.status == "resolved"
    child = _by_path(graph, "pkg/child.py")
    assert _by_path(graph, "importer.py").file_id in graph.reverse[child.file_id]


def test_rename_exclude_and_alias_range_changes(tmp_path: Path, cache: Path):
    root = tmp_path / "repo"
    _write_tree(
        root,
        {
            "pkg/__init__.py": "",
            "pkg/target.py": "def run():\n    return 1\n",
            "importer.py": "import pkg.target as t\n",
        },
    )
    index = CodeIndex(root, cache_base=cache)
    index.build()
    graph = build_graph(index.read_generation())

    (root / "pkg" / "target.py").rename(root / "pkg" / "renamed.py")
    index.update()
    graph = refresh_graph(graph, index.read_generation())
    assert _graph_signature(graph) == _graph_signature(build_graph(index.read_generation()))
    assert _relation(graph, "importer.py", "pkg.target").status == "unresolved"

    (root / "pkg" / "renamed.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (root / "pkg" / "renamed.py").rename(root / "pkg" / "target.py")
    (root / "importer.py").write_text("import pkg.target as renamed\n", encoding="utf-8")
    index.update()
    graph = refresh_graph(graph, index.read_generation())
    assert _graph_signature(graph) == _graph_signature(build_graph(index.read_generation()))
    assert _relation(graph, "importer.py", "pkg.target").alias == "renamed"

    (root / ".gitignore").write_text("pkg/target.py\n", encoding="utf-8")
    index.update()
    graph = refresh_graph(graph, index.read_generation())
    assert _graph_signature(graph) == _graph_signature(build_graph(index.read_generation()))
    assert _relation(graph, "importer.py", "pkg.target").status == "unresolved"


def test_incompatible_keys_force_rebuild(tmp_path: Path, cache: Path):
    root = tmp_path / "repo"
    _write_tree(root, {"a.py": "import b\n", "b.py": "x = 1\n"})
    index = CodeIndex(root, cache_base=cache)
    index.build()
    previous = build_graph(index.read_generation())
    generation = index.read_generation()
    # Force incompatible contract via a shallow copy of previous with bad version.
    object.__setattr__(previous, "contract_version", GRAPH_CONTRACT_VERSION + 1)
    rebuilt = refresh_graph(previous, generation)
    assert rebuilt.contract_version == GRAPH_CONTRACT_VERSION
    assert rebuilt is not previous


def test_published_graph_is_immutable(tmp_path: Path, cache: Path):
    root = tmp_path / "repo"
    _write_tree(root, {"a.py": "import b\n", "b.py": "def run():\n    return 1\n"})
    index = CodeIndex(root, cache_base=cache)
    index.build()
    graph = build_graph(index.read_generation())
    with pytest.raises(AttributeError):
        graph.relations = ()  # type: ignore[misc]
    snapshot = _graph_signature(graph)
    (root / "c.py").write_text("import a\n", encoding="utf-8")
    index.update()
    refreshed = refresh_graph(graph, index.read_generation())
    assert _graph_signature(graph) == snapshot
    assert refreshed is not graph
    assert len(refreshed.nodes) == 3
