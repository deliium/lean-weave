from __future__ import annotations

from pathlib import Path

from lean_weave.config.models import ContextConfig
from lean_weave.context.packing_source import (
    PackingCatalog,
    _python_spans,
    map_rows_from_graph,
    metadata_map_rows,
    promote_range,
    relation_evidence,
    resolve_anchor_span,
    slice_lines,
    unit_overlap,
)
from lean_weave.context.repository import FileSnapshot, Repository
from lean_weave.repository_map.models import (
    DependencyGraph,
    ImportRelation,
    ModuleNode,
    SourceRef,
)


def scan_repo(root: Path, config: ContextConfig | None = None):
    repository = Repository(root, config or ContextConfig(), strict_source=True)
    return repository, repository.scan_retrieval_sources()


def write(root: Path, name: str, content: str | bytes) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def test_slice_lines_preserves_eof_marker():
    text = "a\nb\n"
    assert slice_lines(text, 1, 1) == "a\n"
    assert slice_lines(text, 2, 2) == "b\n"
    no_newline = "a\nb"
    assert slice_lines(no_newline, 2, 2) == "b"
    assert slice_lines(no_newline, 1, 2) == "a\nb"


def test_catalog_includes_multiline_decorator_start(project: Path):
    write(
        project,
        "pkg/core.py",
        "@(\n"
        "    decorator\n"
        ")\n"
        "def decorated():\n"
        "    return 1\n",
    )
    repository, cm = scan_repo(project)
    with cm as scan:
        catalog = PackingCatalog(scan)
        unit, reason = catalog.symbol_unit("pkg/core.py", "decorated", occurrence=0)
        assert reason is None
        assert (unit.start_line, unit.end_line) == (1, 5)
        assert unit.text.startswith("@(\n")
        assert unit.text.endswith("return 1\n")


def test_catalog_keeps_semicolon_siblings_on_one_line(project: Path):
    write(project, "pkg/core.py", "a = 1; b = 2\ndef f():\n    return a + b\n")
    repository, cm = scan_repo(project)
    with cm as scan:
        catalog = PackingCatalog(scan)
        spans = catalog.spans("pkg/core.py")
        assert spans[0].start == spans[0].end == 1
        unit, reason = catalog.range_unit("pkg/core.py", 1, 1)
        assert reason is None
        assert unit.text == "a = 1; b = 2\n"


def test_catalog_promotes_range_to_complete_statement(project: Path):
    write(project, "pkg/core.py", "def f():\n    return 1\n")
    repository, cm = scan_repo(project)
    with cm as scan:
        catalog = PackingCatalog(scan)
        unit, reason = catalog.range_unit("pkg/core.py", 2, 2)
        assert reason is None
        assert (unit.start_line, unit.end_line) == (1, 2)
        assert unit.text == "def f():\n    return 1\n"


def test_catalog_rejects_non_structural_gap(project: Path):
    write(project, "pkg/core.py", "def f():\n    pass\n\n\ndef g():\n    pass\n")
    repository, cm = scan_repo(project)
    with cm as scan:
        catalog = PackingCatalog(scan)
        unit, reason = catalog.range_unit("pkg/core.py", 2, 5)
        assert unit is None
        assert reason == "invalid_structural_range"


def test_non_python_range_is_exact_and_unicode(project: Path):
    write(project, "notes.txt", "α\nβ\nγ\n")
    repository, cm = scan_repo(project)
    with cm as scan:
        catalog = PackingCatalog(scan)
        unit, reason = catalog.range_unit("notes.txt", 2, 3)
        assert reason is None
        assert unit.text == "β\nγ\n"
        assert unit.kind == "range"


def test_crlf_is_canonical_lf(project: Path):
    write(project, "notes.txt", b"a\r\nb\r\n")
    repository, cm = scan_repo(project)
    with cm as scan:
        catalog = PackingCatalog(scan)
        unit, _ = catalog.range_unit("notes.txt", 1, 2)
        assert unit.text == "a\nb\n"


def test_anchor_resolution_prefers_smallest_definition():
    snapshot = FileSnapshot(
        path="pkg/core.py",
        text="class A:\n    def m(self):\n        return 1\n",
        digest="sha256:x",
        size_bytes=0,
    )
    spans = _python_spans(snapshot)
    chosen = resolve_anchor_span(spans, 3)
    assert chosen is not None
    assert chosen.symbol == "A.m"
    promoted = promote_range(spans, 1, 3)
    assert promoted is not None
    assert promoted.symbol == "A"


def test_map_rows_degrade_to_metadata_without_graph(project: Path):
    write(project, "pkg/core.py", "def f():\n    return 1\n")
    write(project, "pkg/other.py", "def g():\n    return 2\n")
    repository, cm = scan_repo(project)
    with cm as scan:
        catalog = PackingCatalog(scan)
        rows = metadata_map_rows(
            catalog, ranked_paths=["pkg/other.py"], rows_max=10
        )
        assert rows[0]["path"] == "pkg/other.py"
        assert rows[0]["origin"] == "ranked"
        assert "pkg/core" in " ".join(row["row"] for row in rows)


def test_unit_overlap_class_before_method(project: Path):
    write(project, "pkg/core.py", "class A:\n    def m(self):\n        return 1\n")
    repository, cm = scan_repo(project)
    with cm as scan:
        catalog = PackingCatalog(scan)
        outer, _ = catalog.symbol_unit("pkg/core.py", "A", occurrence=0)
        inner, _ = catalog.symbol_unit("pkg/core.py", "A.m", occurrence=0)
        assert unit_overlap(outer, inner) == "contains"
        assert unit_overlap(inner, outer) == "contained"


def _graph(scan) -> DependencyGraph:
    digests = {item.path: item.digest for item in scan.snapshots}
    nodes = (
        ModuleNode(
            file_id="f1",
            path="pkg/core.py",
            module_name="pkg.core",
            digest=digests["pkg/core.py"],
            size_bytes=1,
            line_count=1,
            parse_status="ok",
            module_symbol_id="s1",
        ),
        ModuleNode(
            file_id="f2",
            path="pkg/other.py",
            module_name="pkg.other",
            digest=digests["pkg/other.py"],
            size_bytes=1,
            line_count=1,
            parse_status="ok",
            module_symbol_id="s2",
        ),
    )
    relation = ImportRelation(
        importer_id="f1",
        source=SourceRef(
            path="pkg/core.py",
            digest=digests["pkg/core.py"],
            start_line=1,
            end_line=1,
        ),
        statement_ordinal=0,
        name_ordinal=0,
        spelling="other",
        alias=None,
        status="resolved",
        reason=None,
        target_id="f2",
        symbol_id=None,
    )
    return DependencyGraph(
        contract_version=1,
        checkout_key="c",
        policy_key="p",
        extractor_key="e",
        schema_version=1,
        revision="r",
        manifest_sha256="m",
        nodes=nodes,
        symbols=(),
        relations=(relation,),
        forward={"f1": ("f2",), "f2": ()},
        reverse={"f1": (), "f2": ("f1",)},
        symbol_references={},
        neighbors={"f1": ("f2",), "f2": ("f1",)},
    )


def test_graph_map_rows_and_relation_evidence(project: Path):
    write(project, "pkg/core.py", "from pkg import other\n")
    write(project, "pkg/other.py", "def g():\n    return 2\n")
    repository, cm = scan_repo(project)
    with cm as scan:
        graph = _graph(scan)
        rows, revision = map_rows_from_graph(
            graph,
            scan,
            ranked_paths=["pkg/other.py"],
            allowed_paths=None,
            rows_max=10,
        )
        assert revision == "r"
        assert rows[0]["path"] == "pkg/other.py"
        assert rows[0]["origin"] == "ranked"
        evidence = relation_evidence(graph, scan)
        assert evidence
        assert evidence[0].related_path in {"pkg/core.py", "pkg/other.py"}
        assert evidence[0].edge_id.startswith("edge:sha256:")


def test_anchor_resolution_includes_enclosing_declaration():
    snapshot = FileSnapshot(
        path="pkg/core.py",
        text="class A:\n    def m(self):\n        return 1\n",
        digest="sha256:x",
        size_bytes=0,
    )
    spans = _python_spans(snapshot)
    chosen = resolve_anchor_span(spans, 3)
    assert chosen is not None
    assert chosen.symbol == "A.m"
    assert (chosen.start, chosen.end) == (1, 3)
    plain = FileSnapshot(
        path="pkg/free.py",
        text="def f():\n    return 1\n",
        digest="sha256:y",
        size_bytes=0,
    )
    free = resolve_anchor_span(_python_spans(plain), 2)
    assert free is not None
    assert (free.start, free.end) == (1, 2)
