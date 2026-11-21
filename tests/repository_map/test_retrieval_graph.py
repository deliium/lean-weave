"""Retrieval graph projection over a shared admitted scan."""

from __future__ import annotations

from pathlib import Path

from lean_weave.code_index import CodeIndex
from lean_weave.config.models import ContextConfig
from lean_weave.context.repository import Repository
from lean_weave.repository_map import RepositoryMap


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def test_retrieval_graph_filters_and_copies_mappings(tmp_path: Path, cache: Path):
    root = tmp_path / "repo"
    _write_tree(
        root,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "def value():\n    return 1\n",
            "pkg/b.py": "from pkg.a import value\n",
            "pkg/c.py": "from pkg import missing\n",
            "cycle_a.py": "import cycle_b\n",
            "cycle_b.py": "import cycle_a\n",
            "self_imp.py": "import self_imp\n",
            "README.md": "# docs\n",
        },
    )
    index = CodeIndex(root, cache_base=cache)
    index.build()
    service = RepositoryMap(index)
    repository = Repository(root, ContextConfig(), strict_source=True)
    with repository.scan_retrieval_sources() as scan:
        graph = service.graph_for_retrieval_scan(scan)
        assert any(node.path == "pkg/b.py" for node in graph.nodes)
        statuses = {item.status for item in graph.relations}
        assert statuses <= {"resolved"}
        assert all(
            item.target_id is not None and item.target_id != item.importer_id
            for item in graph.relations
        )
        # Mutating the returned adjacency must not affect later map views.
        target = next(iter(graph.forward))
        graph.forward[target] = ()
        again = service.show()
        assert again.rows


def test_retrieval_graph_keeps_circular_resolved_edges(tmp_path: Path, cache: Path):
    root = tmp_path / "repo"
    _write_tree(
        root,
        {
            "a.py": "import b\n",
            "b.py": "import a\n",
        },
    )
    index = CodeIndex(root, cache_base=cache)
    index.build()
    service = RepositoryMap(index)
    repository = Repository(root, ContextConfig(), strict_source=True)
    with repository.scan_retrieval_sources() as scan:
        graph = service.graph_for_retrieval_scan(scan)
        assert len(graph.relations) == 2
        paths = {
            next(n.path for n in graph.nodes if n.file_id == item.importer_id)
            for item in graph.relations
        }
        assert paths == {"a.py", "b.py"}
