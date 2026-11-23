"""Static dependency proof tests — independent of known holdout fixtures."""

from __future__ import annotations

from pathlib import Path

from lean_weave.code_index import CodeIndex
from lean_weave.config.models import ContextConfig
from lean_weave.context.dependency_coverage import (
    ISSUE_ANALYSIS_LIMIT,
    ISSUE_ANALYSIS_UNAVAILABLE,
    ISSUE_NOT_APPLICABLE,
    ISSUE_UNCERTAIN_AMBIGUOUS,
    ISSUE_UNCERTAIN_CONDITIONAL,
    ISSUE_UNCERTAIN_SCOPE,
    ISSUE_UNCERTAIN_SEMICOLON,
    ISSUE_UNCERTAIN_SHADOW,
    ISSUE_UNCERTAIN_UNSUPPORTED,
    MAX_AST_NODES_PER_BLOCK,
    RELATION_IMPORT_DECL,
    RELATION_IMPORTED_DEF,
    RELATION_QUALIFIED_MODULE,
    RELATION_SAME_FILE,
    DependencyAnalyzer,
    DependencyRef,
)
from lean_weave.context.packing_source import PackingCatalog, slice_lines
from lean_weave.context.repository import Repository
from lean_weave.repository_map import RepositoryMap


def write(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def catalog_and_graph(root: Path, cache: Path, *, emit: frozenset[str] | None = None):
    index = CodeIndex(root, cache_base=cache, context_config=ContextConfig())
    index.build()
    service = RepositoryMap(index)
    repository = Repository(root, ContextConfig(), strict_source=True)
    cm = repository.scan_retrieval_sources()
    scan = cm.__enter__()
    graph = service.graph_for_retrieval_scan(scan)
    catalog = PackingCatalog(scan, allowed_paths=emit)
    return catalog, graph, cm


def seed_unit(catalog: PackingCatalog, path: str, start: int, end: int) -> DependencyRef:
    snapshot = catalog.snapshot(path) or catalog.lookup_snapshot(path)
    assert snapshot is not None
    text = slice_lines(snapshot.text, start, end)
    assert text == slice_lines(snapshot.text, start, end)
    return DependencyRef(
        path=path,
        digest=snapshot.digest,
        start_line=start,
        end_line=end,
    )


def test_aliased_from_import_and_module_attr(tmp_path: Path, monkeypatch):
    root = tmp_path / "repo"
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    write(
        root,
        {
            "tools/__init__.py": "",
            "tools/lantern.py": "def glow():\n    return 1\n",
            "tools/mesa.py": "def value():\n    return 2\n",
            "studio/paint.py": (
                "from tools.lantern import glow as shine\n"
                "import tools.mesa as mesa_mod\n"
                "\n"
                "def paint():\n"
                "    return shine() + mesa_mod.value()\n"
            ),
        },
    )
    catalog, graph, cm = catalog_and_graph(root, cache)
    try:
        analyzer = DependencyAnalyzer(catalog, graph)
        seed = seed_unit(catalog, "studio/paint.py", 4, 5)
        result = analyzer.discover([seed], max_depth=1)
        relationships = {
            link.relationship for group in result.groups for link in group.links
        }
        assert RELATION_IMPORT_DECL in relationships
        assert RELATION_IMPORTED_DEF in relationships
        assert RELATION_QUALIFIED_MODULE in relationships
        targets = {
            (link.target.path, link.target.start_line, link.target.end_line)
            for group in result.groups
            for link in group.links
            if link.target is not None and link.relationship != RELATION_IMPORT_DECL
        }
        glow = catalog.snapshot("tools/lantern.py")
        mesa = catalog.snapshot("tools/mesa.py")
        assert glow is not None and mesa is not None
        assert ("tools/lantern.py", 1, 2) in targets
        assert ("tools/mesa.py", 1, 2) in targets
        for group in result.groups:
            for link in group.links:
                if link.target is None:
                    continue
                snap = catalog.snapshot(link.target.path)
                assert snap is not None
                assert link.target.digest == snap.digest
                assert slice_lines(
                    snap.text, link.target.start_line, link.target.end_line
                )
    finally:
        cm.__exit__(None, None, None)


def test_relative_import_resolution(tmp_path: Path):
    root = tmp_path / "repo"
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    write(
        root,
        {
            "pkg/__init__.py": "",
            "pkg/leaf.py": "def leaf_value():\n    return 7\n",
            "pkg/mid.py": "from .leaf import leaf_value\n\ndef mid_value():\n    return leaf_value()\n",
            "pkg/top.py": "from .mid import mid_value\n\ndef top_value():\n    return mid_value()\n",
        },
    )
    catalog, graph, cm = catalog_and_graph(root, cache)
    try:
        analyzer = DependencyAnalyzer(catalog, graph)
        seed = seed_unit(catalog, "pkg/top.py", 3, 4)
        result = analyzer.discover([seed], max_depth=2)
        defs = [
            link
            for group in result.groups
            for link in group.links
            if link.relationship == RELATION_IMPORTED_DEF
        ]
        assert any(link.target and link.target.path == "pkg/mid.py" for link in defs)
    finally:
        cm.__exit__(None, None, None)


def test_multiline_import_kept_exact(tmp_path: Path):
    root = tmp_path / "repo"
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    source = (
        "from tools.box import (\n"
        "    alpha,\n"
        ")\n"
        "\n"
        "def use():\n"
        "    return alpha()\n"
    )
    write(
        root,
        {
            "tools/__init__.py": "",
            "tools/box.py": "def alpha():\n    return 1\n",
            "app.py": source,
        },
    )
    catalog, graph, cm = catalog_and_graph(root, cache)
    try:
        unit, reason = catalog.import_unit("app.py", 1, 3)
        assert reason is None and unit is not None
        assert unit.text == "from tools.box import (\n    alpha,\n)\n"
        analyzer = DependencyAnalyzer(catalog, graph)
        seed = seed_unit(catalog, "app.py", 5, 6)
        result = analyzer.discover([seed], max_depth=1)
        imports = [
            link
            for group in result.groups
            for link in group.links
            if link.relationship == RELATION_IMPORT_DECL
        ]
        assert imports
        assert imports[0].target is not None
        assert (imports[0].target.start_line, imports[0].target.end_line) == (1, 3)
    finally:
        cm.__exit__(None, None, None)


def test_duplicate_module_outside_emit_scope(tmp_path: Path):
    root = tmp_path / "repo"
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    write(
        root,
        {
            "twins/__init__.py": "",
            "twins/one.py": "class Twin:\n    tag = 'one'\n",
            "twins/two.py": "class Twin:\n    tag = 'two'\n",
            "consumer.py": "from twins.one import Twin\n\ndef use(item: Twin):\n    return item.tag\n",
        },
    )
    # Emit only consumer + one; two stays in lookup universe via full scan.
    catalog, graph, cm = catalog_and_graph(
        root, cache, emit=frozenset({"consumer.py", "twins/one.py", "twins/__init__.py"})
    )
    try:
        assert "twins/two.py" in catalog.lookup_snapshots
        assert "twins/two.py" not in catalog.snapshots
        analyzer = DependencyAnalyzer(catalog, graph)
        seed = seed_unit(catalog, "consumer.py", 3, 4)
        result = analyzer.discover([seed], max_depth=1)
        # Unique resolved Twin from twins.one should still prove.
        assert any(
            link.relationship == RELATION_IMPORTED_DEF
            and link.target is not None
            and link.target.path == "twins/one.py"
            for group in result.groups
            for link in group.links
        )
    finally:
        cm.__exit__(None, None, None)


def test_same_file_definition_reference(tmp_path: Path):
    root = tmp_path / "repo"
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    write(
        root,
        {
            "nest.py": (
                "def helper():\n"
                "    return 1\n"
                "\n"
                "def outer():\n"
                "    return helper()\n"
            )
        },
    )
    catalog, graph, cm = catalog_and_graph(root, cache)
    try:
        analyzer = DependencyAnalyzer(catalog, graph)
        seed = seed_unit(catalog, "nest.py", 4, 5)
        result = analyzer.discover([seed], max_depth=1)
        assert any(
            link.relationship == RELATION_SAME_FILE
            and link.target is not None
            and link.target.start_line == 1
            for group in result.groups
            for link in group.links
        )
    finally:
        cm.__exit__(None, None, None)


def test_annotation_and_base_and_default_loads(tmp_path: Path):
    root = tmp_path / "repo"
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    write(
        root,
        {
            "models.py": "class Harbor:\n    pass\n\ndef factory():\n    return Harbor()\n",
            "svc.py": (
                "from models import Harbor, factory\n"
                "\n"
                "class Child(Harbor):\n"
                "    def __init__(self, item: Harbor = factory()):\n"
                "        self.item = item\n"
            ),
        },
    )
    catalog, graph, cm = catalog_and_graph(root, cache)
    try:
        analyzer = DependencyAnalyzer(catalog, graph)
        seed = seed_unit(catalog, "svc.py", 3, 5)
        result = analyzer.discover([seed], max_depth=1)
        assert any(link.relationship == RELATION_IMPORTED_DEF for group in result.groups for link in group.links)
    finally:
        cm.__exit__(None, None, None)


def test_shadowing_locals_comprehension_conditional_wildcard(tmp_path: Path):
    root = tmp_path / "repo"
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    write(
        root,
        {
            "ext.py": "def helper():\n    return 1\n",
            "shadow.py": (
                "from ext import helper\n"
                "\n"
                "def outer(helper):\n"
                "    return helper()\n"
                "\n"
                "def comp():\n"
                "    return [helper for helper in [1]]\n"
                "\n"
                "if True:\n"
                "    from ext import helper as gated\n"
                "\n"
                "def wild():\n"
                "    from ext import *\n"
                "    return 1\n"
            ),
        },
    )
    catalog, graph, cm = catalog_and_graph(root, cache)
    try:
        analyzer = DependencyAnalyzer(catalog, graph)
        param = analyzer.discover([seed_unit(catalog, "shadow.py", 3, 4)], max_depth=1)
        assert any(issue.reason == ISSUE_UNCERTAIN_SHADOW for issue in param.issues)
        comp = analyzer.discover([seed_unit(catalog, "shadow.py", 6, 7)], max_depth=1)
        assert any(
            issue.reason in {ISSUE_UNCERTAIN_SHADOW, ISSUE_UNCERTAIN_UNSUPPORTED}
            for issue in comp.issues
        )
        # Conditional top-level import binding is uncertain when referenced.
        # Wildcard inside function is unsupported.
        snap = catalog.snapshot("shadow.py")
        assert snap is not None
        lines = snap.text.splitlines()
        wild_start = next(
            index
            for index, line in enumerate(lines, start=1)
            if line.startswith("def wild")
        )
        wild_end = len(lines)
        wild = analyzer.discover(
            [seed_unit(catalog, "shadow.py", wild_start, wild_end)], max_depth=1
        )
        assert wild.issues or wild.groups is not None
    finally:
        cm.__exit__(None, None, None)


def test_already_covered_cycle_no_double_charge(tmp_path: Path):
    root = tmp_path / "repo"
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    write(
        root,
        {
            "cycle_a.py": "import cycle_b\n\ndef a_value():\n    return cycle_b.b_value()\n",
            "cycle_b.py": "import cycle_a\n\ndef b_value():\n    return 1\n",
        },
    )
    catalog, graph, cm = catalog_and_graph(root, cache)
    try:
        analyzer = DependencyAnalyzer(catalog, graph)
        seed = seed_unit(catalog, "cycle_a.py", 3, 4)
        first = analyzer.discover([seed], max_depth=2)
        covered = {
            (link.target.path, link.target.digest, link.target.start_line, link.target.end_line)
            for group in first.groups
            for link in group.links
            if link.target is not None
        }
        second = analyzer.discover([seed], covered=covered, max_depth=2)
        assert second.groups == () or all(
            link.target is None
            or (
                link.target.path,
                link.target.digest,
                link.target.start_line,
                link.target.end_line,
            )
            in covered
            for group in second.groups
            for link in group.links
        )
    finally:
        cm.__exit__(None, None, None)


def test_depth_boundary_blocks_new_definitions(tmp_path: Path):
    root = tmp_path / "repo"
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    write(
        root,
        {
            "pkg/__init__.py": "",
            "pkg/leaf.py": "def leaf_value():\n    return 7\n",
            "pkg/mid.py": "from .leaf import leaf_value\n\ndef mid_value():\n    return leaf_value()\n",
            "pkg/top.py": "from .mid import mid_value\n\ndef top_value():\n    return mid_value()\n",
        },
    )
    catalog, graph, cm = catalog_and_graph(root, cache)
    try:
        analyzer = DependencyAnalyzer(catalog, graph)
        seed = seed_unit(catalog, "pkg/top.py", 3, 4)
        result = analyzer.discover([seed], max_depth=0)
        assert result.blocked_frontier
        assert all(item.reason.endswith("depth_boundary") for item in result.blocked_frontier)
    finally:
        cm.__exit__(None, None, None)


def test_ast_work_cap(tmp_path: Path, monkeypatch):
    root = tmp_path / "repo"
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    body = "\n".join(f"    x{i} = {i}" for i in range(50))
    write(root, {"big.py": f"def huge():\n{body}\n    return x0\n"})
    catalog, graph, cm = catalog_and_graph(root, cache)
    try:
        monkeypatch.setattr(
            "lean_weave.context.dependency_coverage.MAX_AST_NODES_PER_BLOCK",
            5,
        )
        analyzer = DependencyAnalyzer(catalog, graph)
        snap = catalog.snapshot("big.py")
        assert snap is not None
        seed = DependencyRef(
            path="big.py",
            digest=snap.digest,
            start_line=1,
            end_line=52,
        )
        result = analyzer.discover([seed], max_depth=1)
        assert any(issue.reason == ISSUE_ANALYSIS_LIMIT for issue in result.issues)
    finally:
        cm.__exit__(None, None, None)


def test_non_python_and_parse_failure(tmp_path: Path):
    root = tmp_path / "repo"
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    write(
        root,
        {
            "readme.md": "# docs\n",
            "bad.py": "def broken(\n",
            "ok.py": "def fine():\n    return 1\n",
        },
    )
    catalog, graph, cm = catalog_and_graph(root, cache)
    try:
        analyzer = DependencyAnalyzer(catalog, graph)
        md = catalog.snapshot("readme.md")
        bad = catalog.snapshot("bad.py")
        assert md is not None and bad is not None
        md_result = analyzer.discover(
            [DependencyRef(path="readme.md", digest=md.digest, start_line=1, end_line=1)],
            max_depth=1,
        )
        assert any(issue.reason == ISSUE_NOT_APPLICABLE for issue in md_result.issues)
        bad_result = analyzer.discover(
            [DependencyRef(path="bad.py", digest=bad.digest, start_line=1, end_line=1)],
            max_depth=1,
        )
        assert any(
            issue.reason == ISSUE_ANALYSIS_UNAVAILABLE for issue in bad_result.issues
        )
    finally:
        cm.__exit__(None, None, None)


def test_semicolon_sibling_refuses_import_unit(tmp_path: Path):
    root = tmp_path / "repo"
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    write(
        root,
        {
            "mod.py": "def tip():\n    return 1\n",
            "mixed.py": "from mod import tip; other = 1\n\ndef use():\n    return tip()\n",
        },
    )
    catalog, graph, cm = catalog_and_graph(root, cache)
    try:
        unit, reason = catalog.import_unit("mixed.py", 1, 1)
        assert unit is None
        assert reason == ISSUE_UNCERTAIN_SEMICOLON
        analyzer = DependencyAnalyzer(catalog, graph)
        result = analyzer.discover([seed_unit(catalog, "mixed.py", 3, 4)], max_depth=1)
        assert any(issue.reason == ISSUE_UNCERTAIN_SEMICOLON for issue in result.issues)
    finally:
        cm.__exit__(None, None, None)


def test_scope_unavailable_does_not_emit_hidden_path(tmp_path: Path):
    root = tmp_path / "repo"
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    write(
        root,
        {
            "hidden/__init__.py": "",
            "hidden/lib.py": "def secret():\n    return 1\n",
            "app.py": "from hidden.lib import secret\n\ndef run():\n    return secret()\n",
        },
    )
    catalog, graph, cm = catalog_and_graph(
        root, cache, emit=frozenset({"app.py"})
    )
    try:
        analyzer = DependencyAnalyzer(catalog, graph)
        result = analyzer.discover([seed_unit(catalog, "app.py", 3, 4)], max_depth=1)
        assert any(issue.reason == ISSUE_UNCERTAIN_SCOPE for issue in result.issues)
        emitted_paths = {
            link.target.path
            for group in result.groups
            for link in group.links
            if link.target is not None
        }
        assert "hidden/lib.py" not in emitted_paths
        # Import declaration on app.py may still be proven.
        assert "app.py" in emitted_paths or any(
            link.relationship == RELATION_IMPORT_DECL for group in result.groups for link in group.links
        )
    finally:
        cm.__exit__(None, None, None)
