"""Bounded, deterministic dependency proof over admitted Python source.

Resolves what a selected fragment actually uses from checked scan evidence and
a scoped import graph. Speculative targets, ranking changes and extra I/O are
out of scope. Results are proof metadata — never prompt text or cache entries.
"""

from __future__ import annotations

import ast
from collections import deque
from dataclasses import dataclass
from typing import Iterable, Iterator, Sequence

from lean_weave.context.packing_source import PackingCatalog, SourceUnit
from lean_weave.context.policy import is_python
from lean_weave.context.python_index import PythonFacts, extract_python_facts
from lean_weave.logging_setup import get_logger
from lean_weave.repository_map.models import DependencyGraph, ImportRelation, ModuleNode

log = get_logger("context.dependency_coverage")

# Bumped only when proof semantics change; serialized into workflow authority
# digests when dependency coverage is enabled.
ANALYZER_CONTRACT_VERSION = 1

MAX_AST_NODES_PER_BLOCK = 4096
MAX_REFERENCES_TOTAL = 256

RELATION_SAME_FILE = "same_file_definition"
RELATION_IMPORT_DECL = "import_declaration"
RELATION_IMPORTED_DEF = "imported_definition"
RELATION_QUALIFIED_MODULE = "qualified_module_definition"

ISSUE_NOT_APPLICABLE = "not_applicable"
ISSUE_ANALYSIS_UNAVAILABLE = "analysis_unavailable"
ISSUE_UNRESOLVED_IMPORT = "unresolved_import"
ISSUE_UNCERTAIN_SHADOW = "dependency_uncertain:shadowed"
ISSUE_UNCERTAIN_AMBIGUOUS = "dependency_uncertain:ambiguous"
ISSUE_UNCERTAIN_UNSUPPORTED = "dependency_uncertain:unsupported"
ISSUE_UNCERTAIN_CONDITIONAL = "dependency_uncertain:conditional"
ISSUE_UNCERTAIN_SCOPE = "dependency_uncertain:scope_unavailable"
ISSUE_UNCERTAIN_SEMICOLON = "dependency_uncertain:semicolon_sibling"
ISSUE_SCAN_INCOMPLETE = "dependency_uncertain:scan_incomplete"
ISSUE_FACTS_TRUNCATED = "dependency_uncertain:facts_truncated"
ISSUE_ANALYSIS_LIMIT = "dependency_analysis_limit"
ISSUE_DEPTH_BLOCKED = "dependency_uncertain:depth_boundary"


@dataclass(frozen=True)
class DependencyRef:
    path: str
    digest: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class DependencyLink:
    origin: DependencyRef
    reference_line: int
    reference_column: int
    target: DependencyRef | None
    relationship: str
    selection_reason: str
    depth: int


@dataclass(frozen=True)
class DependencyIssue:
    origin: DependencyRef
    reason: str
    target: DependencyRef | None = None


@dataclass(frozen=True)
class ProofGroup:
    """Mandatory group: proven definition plus any required import slots."""

    links: tuple[DependencyLink, ...]

    @property
    def additions(self) -> tuple[DependencyLink, ...]:
        return self.links


@dataclass(frozen=True)
class DiscoveryResult:
    groups: tuple[ProofGroup, ...]
    issues: tuple[DependencyIssue, ...]
    blocked_frontier: tuple[DependencyIssue, ...]


@dataclass(frozen=True)
class _Binding:
    kind: str  # import | definition | uncertain
    name: str
    lineno: int
    col: int
    module: str | None = None
    level: int = 0
    imported_name: str | None = None
    alias: str | None = None
    statement_ordinal: int | None = None
    name_ordinal: int | None = None
    start_line: int | None = None
    end_line: int | None = None
    uncertain_reason: str | None = None


@dataclass
class _FileProjection:
    path: str
    digest: str
    text: str
    tree: ast.AST | None
    facts: PythonFacts | None
    status: str
    bindings: dict[str, _Binding]
    import_stmts: tuple[ast.stmt, ...]


def _ref_from_unit(unit: SourceUnit) -> DependencyRef:
    return DependencyRef(
        path=unit.path,
        digest=unit.digest,
        start_line=unit.start_line,
        end_line=unit.end_line,
    )


def _covered_key(ref: DependencyRef) -> tuple[str, str, int, int]:
    return (ref.path, ref.digest, ref.start_line, ref.end_line)


class DependencyAnalyzer:
    """Scan-local binding analysis with breadth-first bounded closure.

    Memoization is strictly operation-local: a new analyzer is required for each
    checked scan. Projections never outlive the owning analyzer instance and are
    never written to ContextCache.
    """

    def __init__(
        self,
        catalog: PackingCatalog,
        graph: DependencyGraph | None,
    ) -> None:
        self.catalog = catalog
        self.graph = graph
        # Keyed only by admitted (path, digest); cleared when the analyzer dies.
        self._projections: dict[tuple[str, str], _FileProjection] = {}
        self._nodes_by_path: dict[str, ModuleNode] = {}
        self._nodes_by_id: dict[str, ModuleNode] = {}
        self._scan_fingerprint = tuple(
            sorted(
                (path, snap.digest)
                for path, snap in catalog.lookup_snapshots.items()
            )
        )
        if graph is not None:
            for node in graph.nodes:
                self._nodes_by_path[node.path] = node
                self._nodes_by_id[node.file_id] = node
        log.debug(
            "[DependencyCoverage] init snapshots=%s lookup=%s graph=%s",
            len(catalog.snapshots),
            len(catalog.lookup_snapshots),
            graph is not None,
        )

    def clear_memo(self) -> None:
        """Drop scan-local projections; required if a catalog is reused across scans."""

        self._projections.clear()
        log.debug("[DependencyCoverage] memo_cleared")

    def discover(
        self,
        seeds: Sequence[DependencyRef | SourceUnit],
        *,
        covered: Iterable[tuple[str, str, int, int]] | None = None,
        max_depth: int = 1,
    ) -> DiscoveryResult:
        if max_depth < 0:
            raise ValueError("max_depth must be >= 0")
        covered_set = set(covered or ())
        seed_refs: list[DependencyRef] = []
        for item in seeds:
            if isinstance(item, SourceUnit):
                seed_refs.append(_ref_from_unit(item))
            else:
                seed_refs.append(item)

        groups: list[ProofGroup] = []
        issues: list[DependencyIssue] = []
        blocked: list[DependencyIssue] = []
        seen_links: set[tuple[str, str, str, str, int, int, str]] = set()
        reference_budget = MAX_REFERENCES_TOTAL
        scanned_refs = 0
        proof_counts: dict[str, int] = {}

        queue: deque[tuple[DependencyRef, int, int]] = deque()
        for order, seed in enumerate(seed_refs):
            queue.append((seed, 0, order))

        while queue:
            origin, depth, seed_order = queue.popleft()
            origin_key = _covered_key(origin)
            already_covered = origin_key in covered_set

            projection = self._projection(origin.path, origin.digest)
            if projection.status == ISSUE_NOT_APPLICABLE:
                issues.append(DependencyIssue(origin=origin, reason=ISSUE_NOT_APPLICABLE))
                continue
            if projection.status != "ok":
                issues.append(DependencyIssue(origin=origin, reason=projection.status))
                continue
            if not self.catalog.complete:
                issues.append(DependencyIssue(origin=origin, reason=ISSUE_SCAN_INCOMPLETE))

            found, local_issues, spent = self._references_in_block(
                projection, origin, reference_budget
            )
            reference_budget -= spent
            scanned_refs += spent
            issues.extend(local_issues)
            if reference_budget <= 0 and found:
                issues.append(
                    DependencyIssue(origin=origin, reason=ISSUE_ANALYSIS_LIMIT)
                )

            ordered = sorted(
                found,
                key=lambda item: (
                    depth,
                    seed_order,
                    item[0],
                    item[1],
                    item[2].path if item[2] is not None else "",
                    item[2].start_line if item[2] is not None else 0,
                    item[2].end_line if item[2] is not None else 0,
                    item[3],
                ),
            )
            for line, col, target, relationship, reason, import_ref in ordered:
                links: list[DependencyLink] = []
                if import_ref is not None:
                    import_key = _covered_key(import_ref)
                    if import_key not in covered_set:
                        links.append(
                            DependencyLink(
                                origin=origin,
                                reference_line=line,
                                reference_column=col,
                                target=import_ref,
                                relationship=RELATION_IMPORT_DECL,
                                selection_reason="referenced_import",
                                depth=depth,
                            )
                        )
                        covered_set.add(import_key)
                if relationship == "issue":
                    issues.append(
                        DependencyIssue(origin=origin, reason=reason, target=target)
                    )
                    if links:
                        groups.append(ProofGroup(links=tuple(links)))
                        proof_counts[RELATION_IMPORT_DECL] = (
                            proof_counts.get(RELATION_IMPORT_DECL, 0) + len(links)
                        )
                    continue
                if target is None and relationship != RELATION_IMPORT_DECL:
                    if links:
                        groups.append(ProofGroup(links=tuple(links)))
                    continue
                link_key = (
                    origin.path,
                    origin.digest,
                    target.path if target is not None else "",
                    relationship,
                    target.start_line if target is not None else 0,
                    target.end_line if target is not None else 0,
                    reason,
                )
                if link_key in seen_links:
                    if links:
                        groups.append(ProofGroup(links=tuple(links)))
                    continue
                seen_links.add(link_key)
                if target is not None and relationship != RELATION_IMPORT_DECL:
                    target_key = _covered_key(target)
                    if depth >= max_depth and target_key not in covered_set:
                        blocked.append(
                            DependencyIssue(
                                origin=origin,
                                reason=ISSUE_DEPTH_BLOCKED,
                                target=target,
                            )
                        )
                        if links:
                            groups.append(ProofGroup(links=tuple(links)))
                        continue
                    if target_key not in covered_set:
                        links.append(
                            DependencyLink(
                                origin=origin,
                                reference_line=line,
                                reference_column=col,
                                target=target,
                                relationship=relationship,
                                selection_reason=reason,
                                depth=depth + 1,
                            )
                        )
                        covered_set.add(target_key)
                        if relationship in {
                            RELATION_SAME_FILE,
                            RELATION_IMPORTED_DEF,
                            RELATION_QUALIFIED_MODULE,
                        }:
                            queue.append((target, depth + 1, seed_order))
                    elif relationship in {
                        RELATION_SAME_FILE,
                        RELATION_IMPORTED_DEF,
                        RELATION_QUALIFIED_MODULE,
                    }:
                        # Traverse already-covered defs when needed; no double charge.
                        queue.append((target, depth + 1, seed_order))

                if links:
                    groups.append(ProofGroup(links=tuple(links)))
                    for link in links:
                        proof_counts[link.relationship] = (
                            proof_counts.get(link.relationship, 0) + 1
                        )

        log.debug(
            "[DependencyCoverage] discover seeds=%s scanned_refs=%s proofs=%s issues=%s blocked=%s",
            len(seed_refs),
            scanned_refs,
            proof_counts,
            len(issues),
            len(blocked),
        )
        return DiscoveryResult(
            groups=tuple(groups),
            issues=tuple(issues),
            blocked_frontier=tuple(blocked),
        )

    def _projection(self, path: str, digest: str) -> _FileProjection:
        key = (path, digest)
        cached = self._projections.get(key)
        if cached is not None:
            return cached
        if not is_python(path):
            projection = _FileProjection(
                path=path,
                digest=digest,
                text="",
                tree=None,
                facts=None,
                status=ISSUE_NOT_APPLICABLE,
                bindings={},
                import_stmts=(),
            )
            self._projections[key] = projection
            return projection
        snapshot = self.catalog.lookup_snapshot(path) or self.catalog.snapshot(path)
        if snapshot is None or snapshot.digest != digest:
            projection = _FileProjection(
                path=path,
                digest=digest,
                text="",
                tree=None,
                facts=None,
                status=ISSUE_ANALYSIS_UNAVAILABLE,
                bindings={},
                import_stmts=(),
            )
            self._projections[key] = projection
            return projection
        facts = self.catalog.python_facts(path)
        if facts is None:
            facts = extract_python_facts(snapshot)
        if facts.status == "ok_truncated":
            status = ISSUE_FACTS_TRUNCATED
        elif facts.status != "ok":
            status = ISSUE_ANALYSIS_UNAVAILABLE
        else:
            status = "ok"
        try:
            tree = ast.parse(snapshot.text, filename=path)
        except (SyntaxError, ValueError, MemoryError, RecursionError):
            projection = _FileProjection(
                path=path,
                digest=digest,
                text=snapshot.text,
                tree=None,
                facts=facts,
                status=ISSUE_ANALYSIS_UNAVAILABLE,
                bindings={},
                import_stmts=(),
            )
            self._projections[key] = projection
            return projection
        bindings, imports = self._top_level_bindings(tree, path)
        projection = _FileProjection(
            path=path,
            digest=digest,
            text=snapshot.text,
            tree=tree,
            facts=facts,
            status=status,
            bindings=bindings,
            import_stmts=imports,
        )
        self._projections[key] = projection
        return projection

    def _top_level_bindings(
        self, tree: ast.AST, path: str
    ) -> tuple[dict[str, _Binding], tuple[ast.stmt, ...]]:
        bindings: dict[str, _Binding] = {}
        imports: list[ast.stmt] = []
        stmt_ordinal = 0
        body = getattr(tree, "body", ())

        def mark_conditional(stmt: ast.stmt) -> None:
            if not isinstance(stmt, (ast.Import, ast.ImportFrom)):
                return
            for alias in stmt.names:
                local = alias.asname or alias.name.split(".")[0]
                bindings[local] = _Binding(
                    kind="uncertain",
                    name=local,
                    lineno=stmt.lineno,
                    col=stmt.col_offset,
                    uncertain_reason=ISSUE_UNCERTAIN_CONDITIONAL,
                )

        for stmt in body:
            if isinstance(stmt, (ast.If, ast.Try, ast.With, ast.AsyncWith)):
                for child in ast.walk(stmt):
                    if isinstance(child, (ast.Import, ast.ImportFrom)):
                        mark_conditional(child)
                continue
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                imports.append(stmt)
                if isinstance(stmt, ast.ImportFrom) and any(
                    alias.name == "*" for alias in stmt.names
                ):
                    bindings["*"] = _Binding(
                        kind="uncertain",
                        name="*",
                        lineno=stmt.lineno,
                        col=stmt.col_offset,
                        uncertain_reason=ISSUE_UNCERTAIN_UNSUPPORTED,
                    )
                    stmt_ordinal += 1
                    continue
                name_ordinal = 0
                for alias in stmt.names:
                    if isinstance(stmt, ast.Import):
                        local = alias.asname or alias.name.split(".")[0]
                        module = alias.name
                        imported = None
                    else:
                        local = alias.asname or alias.name
                        module = stmt.module
                        imported = alias.name
                        if stmt.level and self._relative_climbs_above_package(path, stmt.level):
                            bindings[local] = _Binding(
                                kind="uncertain",
                                name=local,
                                lineno=stmt.lineno,
                                col=stmt.col_offset,
                                uncertain_reason=ISSUE_UNRESOLVED_IMPORT,
                            )
                            name_ordinal += 1
                            continue
                    if local in bindings:
                        bindings[local] = _Binding(
                            kind="uncertain",
                            name=local,
                            lineno=stmt.lineno,
                            col=stmt.col_offset,
                            uncertain_reason=ISSUE_UNCERTAIN_AMBIGUOUS,
                        )
                    else:
                        bindings[local] = _Binding(
                            kind="import",
                            name=local,
                            lineno=stmt.lineno,
                            col=alias.col_offset if hasattr(alias, "col_offset") else stmt.col_offset,
                            module=module,
                            level=getattr(stmt, "level", 0) or 0,
                            imported_name=imported,
                            alias=alias.asname,
                            statement_ordinal=stmt_ordinal,
                            name_ordinal=name_ordinal,
                            start_line=stmt.lineno,
                            end_line=getattr(stmt, "end_lineno", None) or stmt.lineno,
                        )
                    name_ordinal += 1
                stmt_ordinal += 1
                continue
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = stmt.name
                start = stmt.lineno
                for decorator in stmt.decorator_list:
                    start = min(start, getattr(decorator, "lineno", start))
                end = getattr(stmt, "end_lineno", None) or stmt.lineno
                if name in bindings and bindings[name].kind == "definition":
                    bindings[name] = _Binding(
                        kind="uncertain",
                        name=name,
                        lineno=stmt.lineno,
                        col=stmt.col_offset,
                        uncertain_reason=ISSUE_UNCERTAIN_AMBIGUOUS,
                    )
                elif name not in bindings or bindings[name].kind != "uncertain":
                    bindings[name] = _Binding(
                        kind="definition",
                        name=name,
                        lineno=stmt.lineno,
                        col=stmt.col_offset,
                        start_line=start,
                        end_line=end,
                    )
                continue
            # Assignments / other top-level rebinds: mark uncertain if they collide.
            for target in self._assignment_names(stmt):
                if target in bindings:
                    bindings[target] = _Binding(
                        kind="uncertain",
                        name=target,
                        lineno=getattr(stmt, "lineno", 1),
                        col=getattr(stmt, "col_offset", 0),
                        uncertain_reason=ISSUE_UNCERTAIN_AMBIGUOUS,
                    )
        return bindings, tuple(imports)

    @staticmethod
    def _relative_climbs_above_package(path: str, level: int) -> bool:
        parts = path.split("/")
        # Package depth is number of directories; level must not exceed it.
        return level > max(0, len(parts) - 1)

    @staticmethod
    def _assignment_names(stmt: ast.AST) -> tuple[str, ...]:
        names: list[str] = []
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    names.append(target.id)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            names.append(stmt.target.id)
        return tuple(names)

    def _references_in_block(
        self,
        projection: _FileProjection,
        origin: DependencyRef,
        budget: int,
    ) -> tuple[
        list[tuple[int, int, DependencyRef | None, str, str, DependencyRef | None]],
        list[DependencyIssue],
        int,
    ]:
        if projection.tree is None or budget <= 0:
            return [], [], 0
        issues: list[DependencyIssue] = []
        found: list[
            tuple[int, int, DependencyRef | None, str, str, DependencyRef | None]
        ] = []
        spent = 0
        nodes_seen = 0

        for node in ast.walk(projection.tree):
            nodes_seen += 1
            if nodes_seen > MAX_AST_NODES_PER_BLOCK:
                issues.append(
                    DependencyIssue(origin=origin, reason=ISSUE_ANALYSIS_LIMIT)
                )
                break
            lineno = getattr(node, "lineno", None)
            if not isinstance(lineno, int):
                continue
            if lineno < origin.start_line or lineno > origin.end_line:
                continue
            # Skip names that are binding targets in definitions outside load sites.
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if spent >= budget:
                    break
                spent += 1
                resolved = self._resolve_name(
                    projection, origin, node.id, lineno, node.col_offset
                )
                if resolved is not None:
                    found.append(resolved)
                continue
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                if spent >= budget:
                    break
                root = self._attribute_root(node)
                if root is None:
                    continue
                spent += 1
                resolved = self._resolve_attribute(
                    projection, origin, node, root, lineno
                )
                if resolved is not None:
                    found.append(resolved)
        return found, issues, spent

    @staticmethod
    def _attribute_root(node: ast.Attribute) -> ast.Name | None:
        current: ast.AST = node.value
        while isinstance(current, ast.Attribute):
            current = current.value
        return current if isinstance(current, ast.Name) else None

    def _resolve_name(
        self,
        projection: _FileProjection,
        origin: DependencyRef,
        name: str,
        lineno: int,
        col: int,
    ) -> tuple[int, int, DependencyRef | None, str, str, DependencyRef | None] | None:
        shadow = self._shadow_reason(projection, origin, name, lineno)
        if shadow is not None:
            return (lineno, col, None, "issue", shadow, None)

        binding = projection.bindings.get(name)
        if binding is None:
            # Builtin or unknown local — no closure required.
            return None
        if binding.kind == "uncertain":
            return (
                lineno,
                col,
                None,
                "issue",
                binding.uncertain_reason or ISSUE_UNCERTAIN_UNSUPPORTED,
                None,
            )
        if binding.kind == "definition":
            assert binding.start_line is not None and binding.end_line is not None
            target = DependencyRef(
                path=projection.path,
                digest=projection.digest,
                start_line=binding.start_line,
                end_line=binding.end_line,
            )
            return (lineno, col, target, RELATION_SAME_FILE, "same_file_name", None)
        if binding.kind == "import":
            import_ref = self._import_ref(projection, binding)
            if import_ref is None:
                return (
                    lineno,
                    col,
                    None,
                    "issue",
                    ISSUE_UNCERTAIN_SEMICOLON,
                    None,
                )
            target, target_issue = self._resolve_import_target(projection, binding)
            if target_issue is not None:
                return (lineno, col, None, "issue", target_issue, import_ref)
            if target is None:
                # Proven import declaration without a verified external target.
                return (
                    lineno,
                    col,
                    None,
                    RELATION_IMPORT_DECL,
                    "referenced_import_unresolved_target",
                    import_ref,
                )
            return (
                lineno,
                col,
                target,
                RELATION_IMPORTED_DEF,
                "imported_name",
                import_ref,
            )
        return None

    def _resolve_attribute(
        self,
        projection: _FileProjection,
        origin: DependencyRef,
        node: ast.Attribute,
        root: ast.Name,
        lineno: int,
    ) -> tuple[int, int, DependencyRef | None, str, str, DependencyRef | None] | None:
        shadow = self._shadow_reason(projection, origin, root.id, lineno)
        if shadow is not None:
            return (lineno, node.col_offset, None, "issue", shadow, None)
        binding = projection.bindings.get(root.id)
        if binding is None or binding.kind != "import":
            # Attribute on non-imported root: ignore (not rooted in imported module).
            return None
        import_ref = self._import_ref(projection, binding)
        if import_ref is None:
            return (
                lineno,
                node.col_offset,
                None,
                "issue",
                ISSUE_UNCERTAIN_SEMICOLON,
                None,
            )
        # Only one-hop attr on module binding: module.attr
        if not isinstance(node.value, ast.Name):
            return (
                lineno,
                node.col_offset,
                None,
                "issue",
                ISSUE_UNCERTAIN_UNSUPPORTED,
                import_ref,
            )
        target = self._resolve_module_attr(projection, binding, node.attr)
        if target is None:
            return (
                lineno,
                node.col_offset,
                None,
                RELATION_IMPORT_DECL,
                "qualified_attr_unresolved",
                import_ref,
            )
        return (
            lineno,
            node.col_offset,
            target,
            RELATION_QUALIFIED_MODULE,
            "qualified_module_attr",
            import_ref,
        )

    def _import_ref(
        self, projection: _FileProjection, binding: _Binding
    ) -> DependencyRef | None:
        if binding.start_line is None or binding.end_line is None:
            return None
        unit, reason = self.catalog.import_unit(
            projection.path, binding.start_line, binding.end_line
        )
        if unit is None:
            return None if reason else None
        if reason is not None:
            return None
        return DependencyRef(
            path=unit.path,
            digest=unit.digest,
            start_line=unit.start_line,
            end_line=unit.end_line,
        )

    def _resolve_import_target(
        self, projection: _FileProjection, binding: _Binding
    ) -> tuple[DependencyRef | None, str | None]:
        if self.graph is None:
            return None, None
        relation = self._match_relation(projection, binding)
        if relation is None:
            return None, None
        if relation.status != "resolved" or relation.target_id is None:
            return None, None
        target_node = self._nodes_by_id.get(relation.target_id)
        if target_node is None:
            return None, None
        # Lookup may use the full checked scan; emission still requires emit scope.
        if self.catalog.lookup_snapshot(target_node.path) is None:
            return None, None
        if target_node.path not in self.catalog.snapshots:
            return None, ISSUE_UNCERTAIN_SCOPE
        target_proj = self._projection(target_node.path, target_node.digest)
        if target_proj.status != "ok":
            return None, None
        symbol_name = binding.imported_name or binding.name
        if binding.imported_name is None and binding.module and "." in (binding.module or ""):
            # import package.module as alias — module binding, not a symbol.
            return None, None
        matches = [
            item
            for item in target_proj.bindings.values()
            if item.kind == "definition" and item.name == symbol_name
        ]
        if len(matches) != 1:
            return None, ISSUE_UNCERTAIN_AMBIGUOUS if len(matches) > 1 else None
        match = matches[0]
        assert match.start_line is not None and match.end_line is not None
        return (
            DependencyRef(
                path=target_node.path,
                digest=target_node.digest,
                start_line=match.start_line,
                end_line=match.end_line,
            ),
            None,
        )

    def _resolve_module_attr(
        self, projection: _FileProjection, binding: _Binding, attr: str
    ) -> DependencyRef | None:
        if self.graph is None:
            return None
        relation = self._match_relation(projection, binding)
        if relation is None or relation.status != "resolved" or relation.target_id is None:
            return None
        target_node = self._nodes_by_id.get(relation.target_id)
        if target_node is None or target_node.path not in self.catalog.snapshots:
            return None
        target_proj = self._projection(target_node.path, target_node.digest)
        if target_proj.status != "ok":
            return None
        matches = [
            item
            for item in target_proj.bindings.values()
            if item.kind == "definition" and item.name == attr
        ]
        if len(matches) != 1:
            return None
        match = matches[0]
        assert match.start_line is not None and match.end_line is not None
        return DependencyRef(
            path=target_node.path,
            digest=target_node.digest,
            start_line=match.start_line,
            end_line=match.end_line,
        )

    def _match_relation(
        self, projection: _FileProjection, binding: _Binding
    ) -> ImportRelation | None:
        if self.graph is None:
            return None
        importer = self._nodes_by_path.get(projection.path)
        if importer is None or importer.digest != projection.digest:
            return None
        matches: list[ImportRelation] = []
        for relation in self.graph.relations:
            if relation.importer_id != importer.file_id:
                continue
            if relation.source.digest != projection.digest:
                continue
            if binding.statement_ordinal is not None and (
                relation.statement_ordinal != binding.statement_ordinal
            ):
                continue
            if binding.name_ordinal is not None and (
                relation.name_ordinal != binding.name_ordinal
            ):
                continue
            if (
                binding.start_line is not None
                and relation.source.start_line is not None
                and relation.source.start_line != binding.start_line
            ):
                continue
            if (
                binding.end_line is not None
                and relation.source.end_line is not None
                and relation.source.end_line != binding.end_line
            ):
                continue
            # Local binding name: explicit alias, else final spelling segment.
            local = relation.alias or relation.spelling.rsplit(".", 1)[-1]
            spelling_tail = relation.spelling.rsplit(".", 1)[-1]
            if local != binding.name:
                if binding.imported_name and spelling_tail == binding.imported_name:
                    if relation.alias not in {None, binding.name}:
                        continue
                elif relation.spelling != binding.name:
                    continue
            matches.append(relation)
        if len(matches) != 1:
            return None
        return matches[0]

    def _shadow_reason(
        self,
        projection: _FileProjection,
        origin: DependencyRef,
        name: str,
        lineno: int,
    ) -> str | None:
        if projection.tree is None:
            return None
        # Nested function/class/param/comprehension/local import between module and use.
        for node in ast.walk(projection.tree):
            node_line = getattr(node, "lineno", None)
            node_end = getattr(node, "end_lineno", node_line)
            if not isinstance(node_line, int):
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == name and origin.start_line <= node_line <= origin.end_line:
                    if node_line <= lineno <= (node_end or node_line):
                        # Definition itself — not a shadow of an outer load site inside nested body
                        pass
                # Parameters
                for arg in self._iter_args(node):
                    if arg == name and node_line <= lineno <= (node_end or node_line):
                        if origin.start_line <= node_line and origin.end_line >= (node_end or node_line):
                            if node_line > origin.start_line or lineno > node_line:
                                return ISSUE_UNCERTAIN_SHADOW
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                if (
                    node.id == name
                    and origin.start_line <= node.lineno <= origin.end_line
                    and node.lineno <= lineno
                ):
                    # Local assignment before use inside the seed block.
                    if node.lineno != getattr(
                        projection.bindings.get(name), "lineno", None
                    ):
                        return ISSUE_UNCERTAIN_SHADOW
            if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                end = getattr(node, "end_lineno", node_line) or node_line
                if node_line <= lineno <= end:
                    for gen in node.generators:
                        if self._comprehension_binds(gen, name):
                            return ISSUE_UNCERTAIN_SHADOW
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                end = getattr(node, "end_lineno", node_line) or node_line
                if origin.start_line <= node_line and node_line <= origin.end_line:
                    for alias in node.names:
                        local = alias.asname or alias.name.split(".")[0]
                        if local == name and node_line <= lineno:
                            # Nested import inside the seed definition.
                            if node_line > getattr(
                                projection.bindings.get(name), "lineno", 0
                            ):
                                return ISSUE_UNCERTAIN_SHADOW
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                if name in node.names and origin.start_line <= node_line <= origin.end_line:
                    return ISSUE_UNCERTAIN_UNSUPPORTED
        return None

    @staticmethod
    def _iter_args(node: ast.AST) -> Iterator[str]:
        args = getattr(node, "args", None)
        if args is None:
            return
        for item in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
            yield item.arg
        if args.vararg is not None:
            yield args.vararg.arg
        if args.kwarg is not None:
            yield args.kwarg.arg

    @staticmethod
    def _comprehension_binds(gen: ast.comprehension, name: str) -> bool:
        target = gen.target
        if isinstance(target, ast.Name):
            return target.id == name
        for child in ast.walk(target):
            if isinstance(child, ast.Name) and child.id == name:
                return True
        return False

