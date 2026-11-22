"""Materialize ranked references into complete, revision-checked source units.

Ranked retrieval windows are *references*.  This module resolves them against
one admitted :class:`~lean_weave.context.repository.SourceScan` and returns
contiguous canonical slices.  It never slices a definition to fit.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Sequence

from lean_weave.context.models import iter_lines
from lean_weave.context.packing_models import (
    CandidateRef,
    RelationEvidence,
    edge_id_for,
    fragment_id_for,
)
from lean_weave.context.policy import is_python
from lean_weave.context.python_index import (
    DefinitionFact,
    extract_python_facts,
    module_name_for_path,
)
from lean_weave.context.repository import FileSnapshot, SourceScan
from lean_weave.context.retrieval_models import RetrievalHit
from lean_weave.context.retrieval_ranking import RankCandidate
from lean_weave.logging_setup import get_logger
from lean_weave.repository_map.models import DependencyGraph
from lean_weave.repository_map.render import module_row

log = get_logger("context.packing_source")


@dataclass(frozen=True)
class SourceUnit:
    path: str
    start_line: int
    end_line: int
    digest: str
    kind: str
    text: str
    symbol: str | None = None
    reasons: tuple[str, ...] = ()
    covered_selectors: tuple[str, ...] = ()
    prior_ids: tuple[str, ...] = ()

    @property
    def fragment_id(self) -> str:
        return fragment_id_for(
            self.path, self.digest, self.start_line, self.end_line
        )

    @property
    def chars(self) -> int:
        return len(self.text)


@dataclass(frozen=True)
class SourceSpan:
    start: int
    end: int
    kind: str
    symbol: str | None
    order: int


class PackingCatalog:
    """Operation-local admitted source catalog; never persists snapshots."""

    def __init__(
        self,
        scan: SourceScan,
        *,
        allowed_paths: frozenset[str] | None = None,
    ) -> None:
        self.complete = scan.complete
        self.degraded = () if scan.complete else ("source_scan_incomplete",)
        self.snapshots: dict[str, FileSnapshot] = {
            item.path: item
            for item in scan.snapshots
            if allowed_paths is None or item.path in allowed_paths
        }
        self._facts: dict[str, tuple[DefinitionFact, ...]] = {}
        self._spans: dict[str, tuple[SourceSpan, ...]] = {}

    def snapshot(self, path: str) -> FileSnapshot | None:
        return self.snapshots.get(path)

    def paths(self) -> tuple[str, ...]:
        return tuple(self.snapshots)

    def python_paths(self) -> tuple[str, ...]:
        return tuple(path for path in self.snapshots if is_python(path))

    def facts(self, path: str) -> tuple[DefinitionFact, ...]:
        if path not in self._facts:
            snapshot = self.snapshots.get(path)
            if snapshot is None or not is_python(path):
                self._facts[path] = ()
            else:
                facts = extract_python_facts(snapshot)
                self._facts[path] = (
                    facts.definitions if facts.status in {"ok", "ok_truncated"} else ()
                )
        return self._facts[path]

    def spans(self, path: str) -> tuple[SourceSpan, ...]:
        if path not in self._spans:
            snapshot = self.snapshots.get(path)
            if snapshot is None or not is_python(path):
                self._spans[path] = ()
            else:
                self._spans[path] = _python_spans(snapshot)
        return self._spans[path]

    # -- explicit resolution ------------------------------------------
    def symbol_unit(
        self,
        path: str,
        qualified_name: str,
        *,
        occurrence: int | None,
        reasons: tuple[str, ...] = ("explicit_symbol",),
    ) -> tuple[SourceUnit | None, str | None]:
        snapshot = self.snapshots.get(path)
        if snapshot is None:
            return None, "required_source_unavailable"
        facts = [
            item
            for item in self.facts(path)
            if _name_matches(item.local_name, qualified_name)
        ]
        facts.sort(key=lambda item: (item.start_line, item.end_line, item.traversal_order))
        if not facts:
            return None, "required_source_unavailable"
        if occurrence is not None:
            if occurrence >= len(facts):
                return None, "required_source_unavailable"
            fact = facts[occurrence]
        elif len(facts) > 1:
            return None, "required_source_unavailable"
        else:
            fact = facts[0]
        unit = _unit_from_span(
            snapshot,
            start=_decorator_line_start(snapshot, fact.start_line),
            end=fact.end_line,
            kind="definition",
            symbol=fact.local_name,
            reasons=reasons,
            covered_selectors=(qualified_name,),
        )
        return unit, None

    def range_unit(
        self,
        path: str,
        start: int,
        end: int,
        *,
        full_file: bool = False,
        reasons: tuple[str, ...] = ("explicit_range",),
    ) -> tuple[SourceUnit | None, str | None]:
        snapshot = self.snapshots.get(path)
        if snapshot is None:
            return None, "required_source_unavailable"
        line_count = len(iter_lines(snapshot.text))
        if full_file:
            start, end = 1, max(1, line_count)
        if line_count == 0:
            return SourceUnit(
                path=path,
                start_line=1,
                end_line=1,
                digest=snapshot.digest,
                kind="full" if full_file else "range",
                text="",
                reasons=reasons,
                covered_selectors=(f"{path}:{start}-{end}",),
            ), None
        if start < 1 or end > line_count or end < start:
            return None, "invalid_structural_range"
        if full_file:
            unit = _unit_from_span(
                snapshot,
                start=start,
                end=end,
                kind="full",
                symbol=None,
                reasons=reasons,
                covered_selectors=(f"{path}:{start}-{end}",),
            )
            return unit, None
        if not is_python(path):
            unit = _unit_from_span(
                snapshot,
                start=start,
                end=end,
                kind="range",
                symbol=None,
                reasons=reasons,
                covered_selectors=(f"{path}:{start}-{end}",),
            )
            return unit, None
        promoted = promote_range(self.spans(path), start, end)
        if promoted is None:
            return None, "invalid_structural_range"
        unit = _unit_from_span(
            snapshot,
            start=promoted.start,
            end=promoted.end,
            kind=promoted.kind,
            symbol=promoted.symbol,
            reasons=reasons,
            covered_selectors=(f"{path}:{start}-{end}",),
        )
        return unit, None

    def anchor_unit(
        self,
        hit: RetrievalHit,
        candidate: RankCandidate | None,
    ) -> tuple[SourceUnit | None, str | None]:
        snapshot = self.snapshots.get(hit.path)
        if snapshot is None:
            return None, "required_source_unavailable"
        if not is_python(hit.path):
            if hit.start_line is None or hit.end_line is None:
                return None, "required_source_unavailable"
            return (
                _unit_from_span(
                    snapshot,
                    start=hit.start_line,
                    end=hit.end_line,
                    kind="range",
                    symbol=None,
                    reasons=(_dominant_reason(hit),),
                ),
                None,
            )
        anchor_line = None
        if candidate is not None and candidate._anchor is not None:
            anchor_line = candidate._anchor.line
        if anchor_line is None:
            anchor_line = hit.definition_start_line or hit.start_line
        spans = self.spans(hit.path)
        chosen = resolve_anchor_span(spans, anchor_line)
        if chosen is None:
            return None, "required_source_unavailable"
        return (
            _unit_from_span(
                snapshot,
                start=chosen.start,
                end=chosen.end,
                kind=chosen.kind,
                symbol=chosen.symbol,
                reasons=(_dominant_reason(hit),),
            ),
            None,
        )

    def first_statement_unit(self, path: str) -> SourceUnit | None:
        snapshot = self.snapshots.get(path)
        if snapshot is None:
            return None
        spans = self.spans(path)
        for span in spans:
            if span.kind != "definition":
                return _unit_from_span(
                    snapshot,
                    start=span.start,
                    end=span.end,
                    kind="range",
                    symbol=None,
                    reasons=("dependency",),
                )
        return None

    def first_definition_for_path(
        self, path: str, ranked: Sequence[RetrievalHit]
    ) -> SourceUnit | None:
        for hit in ranked:
            if hit.path != path or hit.kind != "symbol" or not hit.qualified_name:
                continue
            unit, _ = self.symbol_unit(
                path,
                hit.qualified_name,
                occurrence=None,
                reasons=("dependency", "ranked_related_definition"),
            )
            if unit is not None:
                return unit
        facts = self.facts(path)
        if facts:
            fact = sorted(
                facts, key=lambda item: (item.start_line, item.end_line, item.traversal_order)
            )[0]
            snapshot = self.snapshots[path]
            return _unit_from_span(
                snapshot,
                start=_decorator_line_start(snapshot, fact.start_line),
                end=fact.end_line,
                kind="definition",
                symbol=fact.local_name,
                reasons=("dependency",),
            )
        return self.first_statement_unit(path)


def _name_matches(local_name: str, qualified_name: str) -> bool:
    return (
        local_name == qualified_name
        or qualified_name.endswith(f".{local_name}")
        or local_name.endswith(f".{qualified_name}")
    )


def _line_starts(text: str) -> tuple[list[int], int]:
    starts = [0]
    for index, char in enumerate(text):
        if char == "\n":
            starts.append(index + 1)
    total = len(starts) - 1 if text.endswith("\n") else len(starts)
    return starts, total


def slice_lines(text: str, start: int, end: int) -> str:
    """Return the exact canonical substring for inclusive 1-based lines."""

    starts, total = _line_starts(text)
    if total == 0:
        return ""
    if start < 1 or end > total or end < start:
        raise ValueError("line range is outside the snapshot")
    start_offset = starts[start - 1]
    end_offset = starts[end] if end < total else len(text)
    return text[start_offset:end_offset]


def _unit_from_span(
    snapshot: FileSnapshot,
    *,
    start: int,
    end: int,
    kind: str,
    symbol: str | None,
    reasons: tuple[str, ...],
    covered_selectors: tuple[str, ...] = (),
    prior_ids: tuple[str, ...] = (),
) -> SourceUnit:
    return SourceUnit(
        path=snapshot.path,
        start_line=start,
        end_line=end,
        digest=snapshot.digest,
        kind=kind,
        text=slice_lines(snapshot.text, start, end),
        symbol=symbol,
        reasons=reasons,
        covered_selectors=covered_selectors,
        prior_ids=prior_ids,
    )


def _decorator_start(node: ast.AST, lines: Sequence[str]) -> int:
    starts = [getattr(node, "lineno", 1)]
    for decorator in getattr(node, "decorator_list", ()):  # noqa: B007
        starts.append(getattr(decorator, "lineno", starts[0]))
    start = min(int(value) for value in starts if isinstance(value, int))
    # Multi-line decorators: ast reports the expression line, not the `@` line.
    while start > 1 and lines[start - 2].lstrip().startswith("@"):
        start -= 1
    return start


def _decorator_line_start(snapshot: FileSnapshot, start: int) -> int:
    lines = snapshot.text.split("\n")
    while start > 1 and lines[start - 2].lstrip().startswith("@"):
        start -= 1
    return start


def _python_spans(snapshot: FileSnapshot) -> tuple[SourceSpan, ...]:
    try:
        tree = ast.parse(snapshot.text, filename=snapshot.path)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return ()
    lines = snapshot.text.split("\n")
    spans: list[SourceSpan] = []
    counter = [0]

    def visit(node: ast.AST, parent: str) -> None:
        local = parent
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            local = f"{parent}.{node.name}" if parent else node.name
        if isinstance(node, ast.stmt):
            start = _decorator_start(node, lines)
            end = int(getattr(node, "end_lineno", None) or getattr(node, "lineno", start))
            kind = (
                "definition"
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                else "statement"
            )
            spans.append(
                SourceSpan(
                    start=start,
                    end=max(start, end),
                    kind=kind,
                    symbol=local if kind == "definition" else None,
                    order=counter[0],
                )
            )
            counter[0] += 1
        for child in ast.iter_child_nodes(node):
            visit(child, local)

    visit(tree, "")
    spans.sort(key=lambda item: (item.start, -(item.end - item.start), item.order))
    return tuple(spans)


def resolve_anchor_span(
    spans: Sequence[SourceSpan], anchor_line: int | None
) -> SourceSpan | None:
    if anchor_line is None:
        return None
    covering = [item for item in spans if item.start <= anchor_line <= item.end]
    if not covering:
        return None
    definitions = [item for item in covering if item.kind == "definition"]
    pool = definitions or covering
    return min(pool, key=lambda item: (item.end - item.start, item.start, item.order))


def promote_range(
    spans: Sequence[SourceSpan], start: int, end: int
) -> SourceSpan | None:
    """Promote a requested range to complete structural units or reject it."""

    covering = [item for item in spans if item.start <= start and item.end >= end]
    if not covering:
        # Accept a contiguous run of adjacent complete statements in one suite.
        top = [
            item
            for item in spans
            if item.start >= start and item.end <= end
        ]
        ordered = sorted(top, key=lambda item: (item.start, item.end, item.order))
        cursor = start
        selected: list[SourceSpan] = []
        for item in ordered:
            if item.start != cursor:
                continue
            selected.append(item)
            cursor = item.end + 1
            if cursor > end:
                break
        if cursor > end and selected:
            return SourceSpan(
                start=selected[0].start,
                end=selected[-1].end,
                kind="range",
                symbol=None,
                order=selected[0].order,
            )
        return None
    definitions = [item for item in covering if item.kind == "definition"]
    pool = definitions or covering
    return min(pool, key=lambda item: (item.end - item.start, item.start, item.order))


def _dominant_reason(hit: RetrievalHit) -> str:
    if hit.ranking_reasons:
        best = max(
            hit.ranking_reasons,
            key=lambda item: (item.weighted_points, item.weight, item.signal),
        )
        return f"rank:{best.signal}"
    return f"rank:{hit.source_kind}"


def unit_overlap(a: SourceUnit, b: SourceUnit) -> str | None:
    if a.path != b.path or a.digest != b.digest:
        return None
    if a.end_line < b.start_line or b.end_line < a.start_line:
        return None
    if a.start_line <= b.start_line and a.end_line >= b.end_line:
        return "contains"
    if b.start_line <= a.start_line and b.end_line >= a.end_line:
        return "contained"
    return "overlap"


def map_rows_from_graph(
    graph: DependencyGraph,
    scan: SourceScan,
    *,
    ranked_paths: Sequence[str],
    allowed_paths: frozenset[str] | None,
    rows_max: int,
) -> tuple[tuple[dict[str, str], ...], str | None]:
    """Return metadata-only module rows and the graph revision."""

    digests = {item.path: item.digest for item in scan.snapshots}
    by_path = {
        node.path: node
        for node in graph.nodes
        if node.path in digests
        and node.digest == digests[node.path]
        and (allowed_paths is None or node.path in allowed_paths)
    }
    ordered: list[str] = []
    seen: set[str] = set()
    for path in ranked_paths:
        if path in by_path and path not in seen:
            ordered.append(path)
            seen.add(path)
    for path in sorted(by_path):
        if path not in seen:
            ordered.append(path)
            seen.add(path)
    rows: list[dict[str, str]] = []
    for path in ordered[:rows_max]:
        node = by_path[path]
        row = module_row(node.module_name, path)
        if row is None:
            continue
        rows.append(
            {
                "path": path,
                "module": node.module_name,
                "row": row,
                "digest": node.digest,
                "origin": "ranked" if path in ranked_paths else "indexed",
            }
        )
    return tuple(rows), graph.revision


def metadata_map_rows(
    catalog: PackingCatalog,
    *,
    ranked_paths: Sequence[str],
    rows_max: int,
) -> tuple[dict[str, str], ...]:
    """Degraded metadata-only map from admitted Python paths (no index)."""

    eligible = set(catalog.python_paths())
    ordered: list[str] = []
    seen: set[str] = set()
    for path in ranked_paths:
        if path in eligible and path not in seen:
            ordered.append(path)
            seen.add(path)
    for path in sorted(eligible):
        if path not in seen:
            ordered.append(path)
            seen.add(path)
    rows: list[dict[str, str]] = []
    for path in ordered[:rows_max]:
        snapshot = catalog.snapshot(path)
        if snapshot is None:
            continue
        module = module_name_for_path(path)
        row = module_row(module, path)
        if row is None:
            continue
        rows.append(
            {
                "path": path,
                "module": module,
                "row": row,
                "digest": snapshot.digest,
                "origin": "ranked" if path in ranked_paths else "metadata",
            }
        )
    return tuple(rows)


def candidate_ref(candidate: RankCandidate) -> CandidateRef:
    return CandidateRef(
        path=candidate.path,
        digest=candidate.source_digest,
        qualified_name=candidate.qualified_name,
        symbol_id=candidate.symbol_id,
        start_line=candidate.start_line,
        end_line=candidate.end_line,
        kind=candidate.kind,
    )


def relation_evidence(
    graph: DependencyGraph | None,
    scan: SourceScan,
    *,
    limit: int = 32,
) -> tuple[RelationEvidence, ...]:
    if graph is None:
        return ()
    digests = {item.path: item.digest for item in scan.snapshots}
    nodes = {node.file_id: node for node in graph.nodes}
    rows: list[RelationEvidence] = []
    seen: set[str] = set()
    for relation in graph.relations:
        if relation.status != "resolved" or relation.target_id is None:
            continue
        importer = nodes.get(relation.importer_id)
        target = nodes.get(relation.target_id)
        if importer is None or target is None:
            continue
        if importer.digest != digests.get(importer.path):
            continue
        if target.digest != digests.get(target.path):
            continue
        if relation.source.digest != importer.digest:
            continue
        start = relation.source.start_line or 1
        end = relation.source.end_line or start
        for direction, anchor_path, anchor_digest, related_path, related_digest in (
            ("import", importer.path, importer.digest, target.path, target.digest),
            ("reverse", target.path, target.digest, importer.path, importer.digest),
        ):
            edge_id = edge_id_for(
                direction=direction,
                anchor_path=anchor_path,
                anchor_digest=anchor_digest,
                related_path=related_path,
                related_digest=related_digest,
                importer_path=importer.path,
                importer_digest=importer.digest,
                start_line=start,
                end_line=end,
            )
            if edge_id in seen:
                continue
            seen.add(edge_id)
            rows.append(
                RelationEvidence(
                    edge_id=edge_id,
                    direction=direction,
                    anchor_path=anchor_path,
                    anchor_digest=anchor_digest,
                    related_path=related_path,
                    related_digest=related_digest,
                    importer_path=importer.path,
                    importer_digest=importer.digest,
                    start_line=start,
                    end_line=end,
                )
            )
            if len(rows) >= limit:
                return tuple(rows)
    return tuple(rows)
