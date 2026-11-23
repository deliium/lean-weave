from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from lean_weave.code_index import CodeIndex
from lean_weave.config.models import ContextConfig, DependencyCoverageConfig
from lean_weave.context.packing_models import (
    DependencyProofLink,
    ExpansionRequest,
    SymbolSelector,
    edge_id_for,
)
from lean_weave.errors import CodeIndexError, InvalidContextRequest
from lean_weave.repository_map import RepositoryMap

from .packing_support import graph_for, make_packer, packing, request


def write(root: Path, name: str, content: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_symbol_expansion_is_complete_and_decorator_inclusive(project: Path):
    write(
        project,
        "pkg/core.py",
        "def anchor():\n    return 0\n\n\n"
        "@(\n    decorator\n)\ndef decorated():\n    return 1\n",
    )
    packer = make_packer(project)
    result, session = packer.open_task(
        request(
            query="zzzz",
            required_symbols=(
                SymbolSelector(path="pkg/core.py", qualified_name="pkg.core.anchor"),
            ),
        )
    )
    assert result.status == "ready" and session is not None
    digest = session.revisions["pkg/core.py"]
    expanded = packer.expand(
        session,
        ExpansionRequest(
            kind="symbol",
            path="pkg/core.py",
            expected_digest=digest,
            qualified_name="pkg.core.decorated",
        ),
    )
    assert expanded.status == "ready"
    fragment = expanded.pack.fragments[0]
    assert fragment.start_line == 5
    assert fragment.text.startswith("@(\n")
    assert fragment.text.endswith("return 1\n")


def test_ambiguous_symbol_requires_occurrence(project: Path):
    write(
        project,
        "pkg/core.py",
        "def anchor():\n    return 0\n\n\ndef dup():\n    return 1\n\n\ndef dup():\n    return 2\n",
    )
    packer = make_packer(project)

    result, session = packer.open_task(
        request(
            query="zzzz",
            required_symbols=(
                SymbolSelector(path="pkg/core.py", qualified_name="pkg.core.anchor"),
            ),
        )
    )
    assert session is not None
    digest = session.revisions["pkg/core.py"]
    ambiguous = packer.expand(
        session,
        ExpansionRequest(
            kind="symbol",
            path="pkg/core.py",
            expected_digest=digest,
            qualified_name="pkg.core.dup",
        ),
    )
    assert ambiguous.status == "insufficient_context"
    exact = packer.expand(
        session,
        ExpansionRequest(
            kind="symbol",
            path="pkg/core.py",
            expected_digest=digest,
            qualified_name="pkg.core.dup",
            occurrence=1,
        ),
    )
    assert exact.status == "ready"
    assert exact.pack.fragments[0].text.endswith("return 2\n")


def test_range_promotes_to_complete_statement(project: Path):
    write(project, "pkg/core.py", "def anchor():\n    return 0\n\n\ndef target():\n    return 1\n")
    packer = make_packer(project)

    result, session = packer.open_task(
        request(
            query="zzzz",
            required_symbols=(
                SymbolSelector(path="pkg/core.py", qualified_name="pkg.core.anchor"),
            ),
        )
    )
    assert session is not None
    expanded = packer.expand(
        session,
        ExpansionRequest(
            kind="range",
            path="pkg/core.py",
            expected_digest=session.revisions["pkg/core.py"],
            start_line=6,
            end_line=6,
        ),
    )
    assert expanded.status == "ready"
    fragment = expanded.pack.fragments[0]
    assert (fragment.start_line, fragment.end_line) == (5, 6)


def test_full_file_requires_justification_and_exact_extent(project: Path):
    write(project, "pkg/core.py", "def anchor():\n    return 0\n")
    with pytest.raises(ValidationError):
        ExpansionRequest(
            kind="range",
            path="pkg/core.py",
            expected_digest="sha256:x",
            start_line=1,
            end_line=2,
            full_file=True,
        )
    packer = make_packer(project)
    result, session = packer.open_task(request(level="l0"))
    assert result.status == "ready" and session is not None
    digest = session.revisions["pkg/core.py"]
    with pytest.raises(InvalidContextRequest):
        packer.expand(
            session,
            ExpansionRequest(
                kind="range",
                path="pkg/core.py",
                expected_digest=digest,
                start_line=1,
                end_line=1,
                full_file=True,
                justification="Need the whole file",
            ),
        )
    expanded = packer.expand(
        session,
        ExpansionRequest(
            kind="range",
            path="pkg/core.py",
            expected_digest=digest,
            start_line=1,
            end_line=2,
            full_file=True,
            justification="Need the whole file",
        ),
    )
    assert expanded.status == "ready"
    assert expanded.pack.fragments[0].kind == "delta"
    assert expanded.pack.fragments[0].text == "def anchor():\n    return 0\n"


def test_non_python_partial_overlap_emits_delta_and_carries_prior(project: Path):
    lines = [f"line-{index}" for index in range(1, 11)]
    lines[4] = "needle"
    write(project, "notes.txt", "\n".join(lines) + "\n")
    packer = make_packer(project)
    result, session = packer.open_task(request(query="needle"))
    assert result.status == "ready" and session is not None
    assert session.revisions["notes.txt"]
    prior = result.pack.fragments[0]
    digest = session.revisions["notes.txt"]
    expanded = packer.expand(
        session,
        ExpansionRequest(
            kind="range",
            path="notes.txt",
            expected_digest=digest,
            start_line=1,
            end_line=prior.end_line,
        ),
    )
    assert expanded.status == "ready"
    delta = expanded.pack.fragments[0]
    assert (delta.start_line, delta.end_line) == (1, prior.start_line - 1)
    assert "line-1" in delta.text
    assert "needle" not in delta.text
    receipt = session.receipts[expanded.receipt_id]
    assert prior.fragment_id in receipt.carry_forward_ids
    delivery = packer.prepare_expansion_delivery(
        session, expanded.receipt_id, receipt.carry_forward_ids
    )
    assert delivery.status == "ready"
    assert [item.fragment_id for item in delivery.pack.fragments] == [
        prior.fragment_id,
        delta.fragment_id,
    ]


def test_nonviable_overlap_fails_atomically(project: Path):
    lines = ["short"] * 10
    lines[4] = "needle"
    lines[9] = "x" * 5000
    write(project, "notes.txt", "\n".join(lines) + "\n")
    packer = make_packer(
        project, config=packing(source_tokens=700, cumulative_source_tokens=8000)
    )
    result, session = packer.open_task(request(query="needle"))
    assert result.status == "ready" and session is not None
    before_coverage = dict(session.coverage)
    before_units = session.cumulative_units
    failed = packer.expand(
        session,
        ExpansionRequest(
            kind="range",
            path="notes.txt",
            expected_digest=session.revisions["notes.txt"],
            start_line=1,
            end_line=10,
        ),
    )
    assert failed.status == "insufficient_context"
    assert failed.reason_codes[0] in {
        "overlap_requires_explicit_rebuild",
        "required_source_over_budget",
    }
    assert session.coverage == before_coverage
    assert session.cumulative_units == before_units


def test_dependency_expansion_resolves_registered_edge(project: Path):
    write(project, "pkg/core.py", "from pkg import other\n\ndef anchor():\n    return 0\n")
    write(project, "pkg/other.py", "def related():\n    return 1\n")

    def load_graph(scan):
        return graph_for(scan)

    packer = make_packer(project, load_graph=load_graph, config=packing(source_tokens=250))
    result, session = packer.open_task(request(query="anchor"))
    assert result.status == "ready" and session is not None
    edge = next(
        item
        for item in session.relations
        if item.direction == "import" and item.related_path == "pkg/other.py"
    )
    expanded = packer.expand(
        session, ExpansionRequest(kind="dependency", edge_id=edge.edge_id)
    )
    assert expanded.status == "ready"
    assert expanded.pack.fragments[0].path == "pkg/other.py"
    assert "related" in expanded.pack.fragments[0].text


def test_unknown_dependency_edge_is_a_request_error(project: Path):
    write(project, "pkg/core.py", "def anchor():\n    return 0\n")
    packer = make_packer(project)
    result, session = packer.open_task(request(query="anchor"))
    assert session is not None
    with pytest.raises(InvalidContextRequest):
        packer.expand(
            session,
            ExpansionRequest(kind="dependency", edge_id="edge:sha256:unknown"),
        )
    assert session.attempts == 0


def test_graph_missing_after_known_edge(project: Path):
    write(project, "pkg/core.py", "from pkg import other\n\ndef anchor():\n    return 0\n")
    write(project, "pkg/other.py", "def related():\n    return 1\n")

    def load_graph(scan):
        return graph_for(scan)

    packer = make_packer(project, load_graph=load_graph, config=packing(source_tokens=250))
    result, session = packer.open_task(request(query="anchor"))
    assert session is not None
    edge = next(item for item in session.relations if item.direction == "import")
    packer.retriever.load_graph = None  # type: ignore[assignment]
    expanded = packer.expand(
        session, ExpansionRequest(kind="dependency", edge_id=edge.edge_id)
    )
    assert expanded.status == "insufficient_context"
    assert "dependency_unavailable" in expanded.reason_codes


def test_unicode_without_trailing_newline_is_exact(project: Path):
    write(project, "pkg/core.py", "def anchor():\n    return 0\n\n\ndef grüß():\n    return 'héllo'")
    packer = make_packer(project)

    result, session = packer.open_task(
        request(
            query="zzzz",
            required_symbols=(
                SymbolSelector(path="pkg/core.py", qualified_name="pkg.core.anchor"),
            ),
        )
    )
    assert session is not None
    expanded = packer.expand(
        session,
        ExpansionRequest(
            kind="symbol",
            path="pkg/core.py",
            expected_digest=session.revisions["pkg/core.py"],
            qualified_name="pkg.core.grüß",
        ),
    )
    assert expanded.status == "ready"
    assert expanded.pack.fragments[0].text == "def grüß():\n    return 'héllo'"


def test_giant_expansion_is_insufficient_not_clipped(project: Path):
    body = "\n".join(f"    value_{index} = {index}" for index in range(400))
    write(
        project,
        "pkg/core.py",
        "def anchor():\n    return 0\n\n\ndef giant():\n" + body + "\n",
    )
    packer = make_packer(project, config=packing(source_tokens=250))

    result, session = packer.open_task(
        request(
            query="zzzz",
            required_symbols=(
                SymbolSelector(path="pkg/core.py", qualified_name="pkg.core.anchor"),
            ),
        )
    )
    assert session is not None
    expanded = packer.expand(
        session,
        ExpansionRequest(
            kind="symbol",
            path="pkg/core.py",
            expected_digest=session.revisions["pkg/core.py"],
            qualified_name="pkg.core.giant",
        ),
    )
    assert expanded.status == "insufficient_context"
    assert "required_source_over_budget" in expanded.reason_codes
    assert expanded.pack.fragments == ()


def test_unresolved_member_relation_is_not_published(project: Path):
    write(project, "pkg/core.py", "from pkg import other\n\ndef anchor():\n    return 0\n")
    write(project, "pkg/other.py", "def related():\n    return 1\n")

    def load_graph(scan):
        graph = graph_for(scan)
        relation = graph.relations[0]
        broken = type(relation)(
            importer_id=relation.importer_id,
            source=relation.source,
            statement_ordinal=relation.statement_ordinal,
            name_ordinal=relation.name_ordinal,
            spelling=relation.spelling,
            alias=relation.alias,
            status="unresolved_member",
            reason="ambiguous",
            target_id=None,
            symbol_id=None,
        )
        return type(graph)(
            contract_version=graph.contract_version,
            checkout_key=graph.checkout_key,
            policy_key=graph.policy_key,
            extractor_key=graph.extractor_key,
            schema_version=graph.schema_version,
            revision=graph.revision,
            manifest_sha256=graph.manifest_sha256,
            nodes=graph.nodes,
            symbols=graph.symbols,
            relations=(broken,),
            forward=graph.forward,
            reverse=graph.reverse,
            symbol_references=graph.symbol_references,
            neighbors=graph.neighbors,
        )

    packer = make_packer(project, load_graph=load_graph)
    result, session = packer.open_task(request(query="anchor"))
    assert session is not None
    assert session.relations == ()


def test_dependency_direction_is_part_of_the_witness(project: Path):
    write(project, "pkg/core.py", "from pkg import other\n\ndef anchor():\n    return 0\n")
    write(project, "pkg/other.py", "def related():\n    return 1\n")

    def load_graph(scan):
        return graph_for(scan)

    packer = make_packer(
        project, load_graph=load_graph, config=packing(source_tokens=250)
    )
    result, session = packer.open_task(request(query="anchor"))
    assert session is not None
    import_edge = next(
        item
        for item in session.relations
        if item.direction == "import" and item.related_path == "pkg/other.py"
    )
    reverse_edge = next(
        item
        for item in session.relations
        if item.direction == "reverse" and item.related_path == "pkg/core.py"
    )
    reverse = packer.expand(
        session, ExpansionRequest(kind="dependency", edge_id=reverse_edge.edge_id)
    )
    assert reverse.status == "no_change"

    wrong = edge_id_for(
        direction="import",
        anchor_path=import_edge.related_path,
        anchor_digest=import_edge.related_digest,
        related_path=import_edge.anchor_path,
        related_digest=import_edge.anchor_digest,
        importer_path=import_edge.importer_path,
        importer_digest=import_edge.importer_digest,
        start_line=import_edge.start_line,
        end_line=import_edge.end_line,
    )
    attempted = session.attempts
    with pytest.raises(InvalidContextRequest):
        packer.expand(session, ExpansionRequest(kind="dependency", edge_id=wrong))
    assert session.attempts == attempted


def test_delivery_rejects_missing_receipt_extra_ids_and_closed_session(
    basic_repo: Path,
):
    packer = make_packer(basic_repo)
    opened, session = packer.open_task(request())
    assert session is not None
    digest = session.revisions["pkg/core.py"]
    expanded = packer.expand(
        session,
        ExpansionRequest(
            kind="symbol",
            path="pkg/core.py",
            expected_digest=digest,
            qualified_name="pkg.core.Widget",
        ),
    )
    assert expanded.status == "ready" and expanded.receipt_id is not None
    with pytest.raises(InvalidContextRequest):
        packer.prepare_expansion_delivery(
            session, "pack-receipt:missing", []
        )
    with pytest.raises(InvalidContextRequest):
        packer.prepare_expansion_delivery(
            session, expanded.receipt_id, ["frag:sha256:extra"]
        )
    session.close()
    with pytest.raises(InvalidContextRequest):
        packer.prepare_expansion_delivery(session, expanded.receipt_id, [])


def test_delivery_after_source_mutation_is_stale(basic_repo: Path):
    packer = make_packer(basic_repo)
    opened, session = packer.open_task(request())
    assert session is not None
    digest = session.revisions["pkg/core.py"]
    expanded = packer.expand(
        session,
        ExpansionRequest(
            kind="symbol",
            path="pkg/core.py",
            expected_digest=digest,
            qualified_name="pkg.core.Widget",
        ),
    )
    assert expanded.status == "ready" and expanded.receipt_id is not None
    (basic_repo / "pkg/core.py").write_text(
        "class Widget:\n    def render(self):\n        return 'changed'\n"
    )
    stale = packer.prepare_expansion_delivery(
        session, expanded.receipt_id, []
    )
    assert stale.status == "stale_reference"
    assert not session.open


def _dependency_expand_fixture(tmp_path: Path):
    root = tmp_path / "repo"
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    write(root, "models/__init__.py", "")
    write(
        root,
        "models/widget.py",
        "class HarborWidget:\n    def spin(self):\n        return 1\n",
    )
    write(root, "service/__init__.py", "")
    write(
        root,
        "service/runner.py",
        "from models.widget import HarborWidget\n\n"
        "def keep():\n    return 0\n\n"
        "def activate(item: HarborWidget):\n    return item.spin()\n",
    )
    index = CodeIndex(root, cache_base=cache, context_config=ContextConfig())
    index.build()
    service = RepositoryMap(index)

    def load_graph(scan):
        return service.graph_for_retrieval_scan(scan)

    packer = make_packer(
        root,
        load_graph=load_graph,
        dependency_coverage=DependencyCoverageConfig(enabled=True, max_depth=1),
        # Unrelated query + no map rows: initial pack is only the required keep().
        config=packing(map_tokens=0),
    )
    opened, session = packer.open_task(
        request(
            query="zzzz_unrelated_token",
            required_symbols=(
                SymbolSelector(
                    path="service/runner.py",
                    qualified_name="service.runner.keep",
                ),
            ),
        )
    )
    assert opened.status == "ready" and session is not None
    digest = session.revisions["service/runner.py"]
    expanded = packer.expand(
        session,
        ExpansionRequest(
            kind="symbol",
            path="service/runner.py",
            expected_digest=digest,
            qualified_name="service.runner.activate",
        ),
    )
    return root, packer, session, expanded


def test_enabled_expansion_binds_dependency_proof_and_delivery_rechecks(tmp_path: Path):
    _root, packer, session, expanded = _dependency_expand_fixture(tmp_path)
    assert expanded.status == "ready" and expanded.receipt_id is not None
    receipt = session.receipts[expanded.receipt_id]
    assert receipt.analyzer_contract_version == 1
    assert receipt.seed_path == "service/runner.py"
    assert receipt.dependency_links
    assert any(
        link.relationship == "imported_definition" and link.path == "models/widget.py"
        for link in receipt.dependency_links
    )
    assert any(
        link.relationship == "import_declaration" and link.path == "service/runner.py"
        for link in receipt.dependency_links
    )
    assert session.dependency_definitions_added >= 1
    assert session.dependency_charged_tokens > 0
    delivery = packer.prepare_expansion_delivery(
        session, expanded.receipt_id, list(receipt.carry_forward_ids)
    )
    assert delivery.status == "ready"

    # Rematerialize would still succeed for original refs; forged relationship must refuse.
    forged = receipt.model_copy(
        update={
            "dependency_links": tuple(
                DependencyProofLink(
                    path=link.path,
                    digest=link.digest,
                    start_line=link.start_line,
                    end_line=link.end_line,
                    relationship="forged_relationship",
                    depth=link.depth,
                    reference_line=link.reference_line,
                )
                for link in receipt.dependency_links
            )
        }
    )
    session.receipts[receipt.receipt_id] = forged
    refused = packer.prepare_expansion_delivery(
        session, expanded.receipt_id, list(receipt.carry_forward_ids)
    )
    assert refused.status == "insufficient_context"
    assert "dependency_unavailable" in refused.reason_codes


def test_delivery_rejects_when_unique_dependency_becomes_ambiguous(tmp_path: Path):
    root, packer, session, expanded = _dependency_expand_fixture(tmp_path)
    assert expanded.status == "ready" and expanded.receipt_id is not None
    receipt = session.receipts[expanded.receipt_id]
    assert receipt.dependency_links
    # Duplicate top-level definition: target digest changes → stale via rematerialize
    # or proof path; either way delivery must not dispatch.
    write(
        root,
        "models/widget.py",
        "class HarborWidget:\n    def spin(self):\n        return 1\n\n"
        "class HarborWidget:\n    def spin(self):\n        return 0\n",
    )
    outcome = packer.prepare_expansion_delivery(
        session, expanded.receipt_id, list(receipt.carry_forward_ids)
    )
    assert outcome.status in {"stale_reference", "insufficient_context"}
    assert outcome.pack is None or not outcome.pack.messages


def test_disabled_receipt_omits_dependency_proof_fields(basic_repo: Path):
    packer = make_packer(basic_repo)
    opened, session = packer.open_task(request())
    assert session is not None
    digest = session.revisions["pkg/core.py"]
    expanded = packer.expand(
        session,
        ExpansionRequest(
            kind="symbol",
            path="pkg/core.py",
            expected_digest=digest,
            qualified_name="pkg.core.Widget",
        ),
    )
    assert expanded.status == "ready" and expanded.receipt_id is not None
    receipt = session.receipts[expanded.receipt_id]
    assert receipt.analyzer_contract_version is None
    assert receipt.dependency_links == ()
    assert receipt.seed_path is None
    assert session.dependency_definitions_added == 0


def test_enabled_reverse_dependency_edge_is_unavailable(project: Path):
    write(project, "pkg/core.py", "from pkg import other\n\ndef anchor():\n    return 0\n")
    write(project, "pkg/other.py", "def related():\n    return 1\n")

    def load_graph(scan):
        return graph_for(scan)

    packer = make_packer(
        project,
        load_graph=load_graph,
        dependency_coverage=DependencyCoverageConfig(enabled=True, max_depth=1),
        config=packing(source_tokens=250, map_tokens=0),
    )
    result, session = packer.open_task(request(query="anchor"))
    assert session is not None
    reverse_edge = next(
        item
        for item in session.relations
        if item.direction == "reverse" and item.related_path == "pkg/core.py"
    )
    refused = packer.expand(
        session, ExpansionRequest(kind="dependency", edge_id=reverse_edge.edge_id)
    )
    assert refused.status == "insufficient_context"
    assert "dependency_unavailable" in refused.reason_codes


def test_already_covered_root_still_delivers_new_dependency_closure(tmp_path: Path):
    root = tmp_path / "repo"
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    write(root, "models/__init__.py", "")
    write(
        root,
        "models/widget.py",
        "class HarborWidget:\n    def spin(self):\n        return 1\n",
    )
    write(root, "service/__init__.py", "")
    write(
        root,
        "service/runner.py",
        "from models.widget import HarborWidget\n\n"
        "def activate(item: HarborWidget):\n    return item.spin()\n",
    )
    index = CodeIndex(root, cache_base=cache, context_config=ContextConfig())
    index.build()
    service = RepositoryMap(index)
    packer = make_packer(
        root,
        load_graph=lambda scan: service.graph_for_retrieval_scan(scan),
        dependency_coverage=DependencyCoverageConfig(enabled=True, max_depth=0),
        config=packing(map_tokens=0),
    )
    opened, session = packer.open_task(
        request(
            query="zzzz",
            required_symbols=(
                SymbolSelector(
                    path="service/runner.py",
                    qualified_name="service.runner.activate",
                ),
            ),
        )
    )
    assert opened.status == "ready" and session is not None
    assert "models/widget.py" not in {ref.path for ref in session.fragments.values()}
    # Raise depth so a later expand can admit the previously blocked definition.
    deeper = DependencyCoverageConfig(enabled=True, max_depth=1)
    packer.context_config = packer.context_config.model_copy(
        update={"dependency_coverage": deeper}
    )
    packer.repository.config = packer.context_config
    digest = session.revisions["service/runner.py"]
    expanded = packer.expand(
        session,
        ExpansionRequest(
            kind="symbol",
            path="service/runner.py",
            expected_digest=digest,
            qualified_name="service.runner.activate",
        ),
    )
    assert expanded.status == "ready" and expanded.receipt_id is not None
    paths = {item.path for item in expanded.pack.fragments}
    assert "models/widget.py" in paths
    receipt = session.receipts[expanded.receipt_id]
    assert any(
        link.relationship == "imported_definition" and link.path == "models/widget.py"
        for link in receipt.dependency_links
    )


def test_narrower_expand_filter_cannot_hide_required_dependency(tmp_path: Path):
    root = tmp_path / "repo"
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    write(root, "models/__init__.py", "")
    write(
        root,
        "models/widget.py",
        "class HarborWidget:\n    def spin(self):\n        return 1\n",
    )
    write(root, "service/__init__.py", "")
    write(
        root,
        "service/runner.py",
        "from models.widget import HarborWidget\n\n"
        "def keep():\n    return 0\n\n"
        "def activate(item: HarborWidget):\n    return item.spin()\n",
    )
    index = CodeIndex(root, cache_base=cache, context_config=ContextConfig())
    index.build()
    service = RepositoryMap(index)
    packer = make_packer(
        root,
        load_graph=lambda scan: service.graph_for_retrieval_scan(scan),
        dependency_coverage=DependencyCoverageConfig(enabled=True, max_depth=1),
        config=packing(map_tokens=0),
    )
    opened, session = packer.open_task(
        request(
            query="zzzz_unrelated_token",
            required_symbols=(
                SymbolSelector(
                    path="service/runner.py",
                    qualified_name="service.runner.keep",
                ),
            ),
        )
    )
    assert opened.status == "ready" and session is not None
    digest = session.revisions["service/runner.py"]
    refused = packer.expand(
        session,
        ExpansionRequest(
            kind="symbol",
            path="service/runner.py",
            expected_digest=digest,
            qualified_name="service.runner.activate",
        ),
        read_filter=lambda path: path != "models/widget.py",
    )
    assert refused.status == "insufficient_context"
    assert "dependency_unavailable" in refused.reason_codes
