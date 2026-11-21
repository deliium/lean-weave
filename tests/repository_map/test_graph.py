from __future__ import annotations

from pathlib import Path

from lean_weave.code_index import CodeIndex
from lean_weave.repository_map import build_graph


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _generation(root: Path, cache: Path, files: dict[str, str]):
    _write_tree(root, files)
    index = CodeIndex(root, cache_base=cache)
    index.build()
    return index, index.read_generation()


def _by_path(graph, path: str):
    return next(node for node in graph.nodes if node.path == path)


def _imports_from(graph, path: str):
    node = _by_path(graph, path)
    return [item for item in graph.relations if item.importer_id == node.file_id]


def test_absolute_dotted_alias_and_multi_name(tmp_path: Path, cache: Path):
    root = tmp_path / "repo"
    _index, generation = _generation(
        root,
        cache,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "def value():\n    return 1\n",
            "pkg/b.py": "def value():\n    return 2\n",
            "app.py": (
                "import pkg.a as alpha\n"
                "import pkg.b, json\n"
                "from pkg.a import value as V\n"
            ),
        },
    )
    graph = build_graph(generation)
    imports = _imports_from(graph, "app.py")
    assert [(item.spelling, item.alias, item.status) for item in imports] == [
        ("pkg.a", "alpha", "resolved"),
        ("pkg.b", None, "resolved"),
        ("json", None, "unresolved"),
        ("pkg.a.value", "V", "resolved"),
    ]
    assert imports[2].reason == "not_indexed"
    a = _by_path(graph, "pkg/a.py")
    assert imports[0].target_id == a.file_id
    assert imports[3].target_id == a.file_id
    assert imports[3].symbol_id is not None
    assert imports[0].source.start_line == 1
    assert imports[3].source.start_line == 3


def test_init_pyi_and_relative_imports(tmp_path: Path, cache: Path):
    root = tmp_path / "repo"
    _index, generation = _generation(
        root,
        cache,
        {
            "pkg/__init__.pyi": "class Exposed: ...\n",
            "pkg/child.py": "def X():\n    return 1\n",
            "pkg/nested/__init__.py": "from ..child import X\nfrom . import sibling\n",
            "pkg/nested/sibling.py": "from ....escape import X\n",
            "pkg/nested/deep.py": "from . import sibling\nfrom ..child import X\n",
        },
    )
    graph = build_graph(generation)
    nested = _imports_from(graph, "pkg/nested/__init__.py")
    assert nested[0].status == "resolved"
    assert nested[0].target_id == _by_path(graph, "pkg/child.py").file_id
    assert nested[1].status == "resolved"
    assert nested[1].target_id == _by_path(graph, "pkg/nested/sibling.py").file_id

    escaping = _imports_from(graph, "pkg/nested/sibling.py")
    assert escaping[0].status == "unresolved"
    assert escaping[0].reason == "invalid_relative"

    deep = _imports_from(graph, "pkg/nested/deep.py")
    assert [item.status for item in deep] == ["resolved", "resolved"]


def test_namespace_child_and_missing_wildcard(tmp_path: Path, cache: Path):
    root = tmp_path / "repo"
    _index, generation = _generation(
        root,
        cache,
        {
            "ns/leaf.py": "def Y():\n    return 1\n",
            "user.py": (
                "import ns.leaf\n"
                "from ns.leaf import Y, missing\n"
                "from ns.leaf import *\n"
                "from missing import *\n"
            ),
        },
    )
    graph = build_graph(generation)
    imports = _imports_from(graph, "user.py")
    assert imports[0].status == "resolved"
    assert imports[1].status == "resolved" and imports[1].symbol_id is not None
    assert imports[2].status == "unresolved" and imports[2].reason == "unresolved_member"
    assert imports[3].status == "resolved" and imports[3].reason == "wildcard_module_only"
    assert imports[4].status == "unresolved" and imports[4].reason == "not_indexed"


def test_py_pyi_and_src_collisions(tmp_path: Path, cache: Path):
    root = tmp_path / "repo"
    _index, generation = _generation(
        root,
        cache,
        {
            "mod.py": "def A():\n    return 1\n",
            "mod.pyi": "def A() -> int: ...\n",
            "src/util.py": "def U():\n    return 1\n",
            "util.py": "def U():\n    return 2\n",
            "client.py": "import mod\nimport util\n",
        },
    )
    graph = build_graph(generation)
    imports = _imports_from(graph, "client.py")
    assert imports[0].status == "ambiguous" and imports[0].reason == "ambiguous_module"
    assert len(imports[0].candidates) == 2
    assert imports[1].status == "ambiguous"
    for node in graph.nodes:
        if node.path in {"mod.py", "mod.pyi", "src/util.py", "util.py"}:
            assert graph.reverse[node.file_id] == ()


def test_ordinary_module_blocks_child_but_supplies_member(tmp_path: Path, cache: Path):
    root = tmp_path / "repo"
    _index, generation = _generation(
        root,
        cache,
        {
            "pkg.py": "def name():\n    return 1\ndef helper():\n    return 1\n",
            "pkg/child.py": "def CHILD():\n    return 1\n",
            "user.py": "import pkg.child\nfrom pkg import name\nfrom pkg import missing\n",
        },
    )
    graph = build_graph(generation)
    imports = _imports_from(graph, "user.py")
    assert imports[0].status == "unresolved" and imports[0].reason == "non_package_parent"
    assert imports[0].target_id is None
    assert imports[1].status == "resolved" and imports[1].symbol_id is not None
    assert imports[1].target_id == _by_path(graph, "pkg.py").file_id
    assert imports[2].status == "unresolved" and imports[2].reason == "unresolved_member"
    child = _by_path(graph, "pkg/child.py")
    assert graph.reverse[child.file_id] == ()
    pkg = _by_path(graph, "pkg.py")
    assert _by_path(graph, "user.py").file_id in graph.reverse[pkg.file_id]


def test_colliding_prefix_makes_child_ambiguous(tmp_path: Path, cache: Path):
    root = tmp_path / "repo"
    _index, generation = _generation(
        root,
        cache,
        {
            "pkg.py": "def X():\n    return 1\n",
            "pkg.pyi": "def X() -> int: ...\n",
            "pkg/child.py": "def Y():\n    return 1\n",
            "user.py": "import pkg.child\n",
        },
    )
    graph = build_graph(generation)
    relation = _imports_from(graph, "user.py")[0]
    assert relation.status == "ambiguous" and relation.reason == "ambiguous_parent"
    assert graph.reverse[_by_path(graph, "pkg/child.py").file_id] == ()


def test_child_vs_definition_ambiguity_and_repeated_defs(tmp_path: Path, cache: Path):
    root = tmp_path / "repo"
    _index, generation = _generation(
        root,
        cache,
        {
            "pkg/__init__.py": "def item():\n    return 1\n",
            "pkg/item.py": "def Z():\n    return 1\n",
            "dup.py": "def twin():\n    return 1\ndef twin():\n    return 2\n",
            "user.py": "from pkg import item\nfrom dup import twin\n",
        },
    )
    graph = build_graph(generation)
    imports = _imports_from(graph, "user.py")
    assert imports[0].status == "ambiguous"
    assert imports[0].reason == "ambiguous_child_or_member"
    assert imports[1].status == "ambiguous"
    assert imports[1].reason == "ambiguous_member"


def test_cycles_self_imports_and_reverse(tmp_path: Path, cache: Path):
    root = tmp_path / "repo"
    _index, generation = _generation(
        root,
        cache,
        {
            "a.py": "import b\n",
            "b.py": "import a\nimport b\n",
        },
    )
    graph = build_graph(generation)
    a = _by_path(graph, "a.py")
    b = _by_path(graph, "b.py")
    assert graph.forward[a.file_id] == (b.file_id,)
    assert graph.forward[b.file_id] == (a.file_id, b.file_id)
    assert graph.reverse[a.file_id] == (b.file_id,)
    assert graph.reverse[b.file_id] == (a.file_id, b.file_id)
    assert a.file_id not in graph.neighbors[a.file_id]
    assert b.file_id not in graph.neighbors[b.file_id]
    assert a.file_id in graph.neighbors[b.file_id]


def test_no_dynamic_call_edges(tmp_path: Path, cache: Path):
    root = tmp_path / "repo"
    _index, generation = _generation(
        root,
        cache,
        {
            "target.py": "def run():\n    return 1\n",
            "user.py": (
                "import importlib\n"
                "importlib.import_module('target')\n"
                "target.run()\n"
            ),
        },
    )
    graph = build_graph(generation)
    imports = _imports_from(graph, "user.py")
    assert len(imports) == 1
    assert imports[0].spelling == "importlib"
    assert imports[0].status == "unresolved"
    assert graph.forward[_by_path(graph, "user.py").file_id] == ()
