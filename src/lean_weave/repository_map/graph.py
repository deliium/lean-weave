"""Build and incrementally refresh static import dependency graphs."""

from __future__ import annotations

from dataclasses import dataclass

from lean_weave.code_index.models import (
    FileRecord,
    IndexGeneration,
    derive_module_id,
    derive_symbol_id,
)
from lean_weave.logging_setup import get_logger
from lean_weave.repository_map.models import (
    GRAPH_CONTRACT_VERSION,
    DependencyGraph,
    ImportRelation,
    MapSymbol,
    ModuleNode,
    ResolutionCandidate,
    SourceRef,
)

log = get_logger("repository_map")


def _is_identifier_module(name: str) -> bool:
    if not name:
        return False
    return all(part.isidentifier() for part in name.split("."))


def _is_package_init(path: str) -> bool:
    return path.endswith("/__init__.py") or path.endswith("/__init__.pyi") or path in {
        "__init__.py",
        "__init__.pyi",
    }


def _proper_prefixes(module: str) -> tuple[str, ...]:
    parts = module.split(".")
    if len(parts) <= 1:
        return ()
    return tuple(".".join(parts[:index]) for index in range(1, len(parts)))


@dataclass(frozen=True)
class _Catalogs:
    nodes: dict[str, ModuleNode]
    by_module: dict[str, tuple[str, ...]]
    top_level: dict[tuple[str, str], tuple[str, ...]]
    symbols: dict[str, MapSymbol]
    records: dict[str, FileRecord]


def _module_source(record: FileRecord) -> SourceRef:
    empty = record.line_count == 0
    return SourceRef(
        path=record.path,
        digest=record.digest,
        start_line=None if empty else 1,
        end_line=None if empty else record.line_count,
    )


def _build_catalogs(records: tuple[FileRecord, ...]) -> _Catalogs:
    nodes: dict[str, ModuleNode] = {}
    module_buckets: dict[str, list[str]] = {}
    top_level: dict[tuple[str, str], list[str]] = {}
    symbols: dict[str, MapSymbol] = {}
    by_id: dict[str, FileRecord] = {}

    for record in sorted(records, key=lambda item: item.path):
        by_id[record.file_id] = record
        module_symbol_id = derive_module_id(record.file_id)
        node = ModuleNode(
            file_id=record.file_id,
            path=record.path,
            module_name=record.module_name,
            digest=record.digest,
            size_bytes=record.size_bytes,
            line_count=record.line_count,
            parse_status=record.parse_status,
            module_symbol_id=module_symbol_id,
        )
        nodes[record.file_id] = node
        if _is_identifier_module(record.module_name):
            module_buckets.setdefault(record.module_name, []).append(record.file_id)

        if record.parse_status not in {"ok"}:
            continue
        for fact in record.facts.definitions:
            symbol_id = derive_symbol_id(
                record.file_id, fact.local_name, fact.kind, fact.occurrence
            )
            qualified = (
                f"{record.module_name}.{fact.local_name}"
                if record.module_name
                else fact.local_name
            )
            symbol = MapSymbol(
                symbol_id=symbol_id,
                file_id=record.file_id,
                qualified_name=qualified,
                simple_name=fact.simple_name,
                kind=fact.kind.value,
                parent=fact.parent,
                signature=fact.signature,
                source=SourceRef(
                    path=record.path,
                    digest=record.digest,
                    start_line=fact.start_line,
                    end_line=fact.end_line,
                ),
                occurrence=fact.occurrence,
            )
            symbols[symbol_id] = symbol
            if fact.parent is None:
                key = (record.file_id, fact.simple_name)
                top_level.setdefault(key, []).append(symbol_id)

    by_module = {
        name: tuple(sorted(ids, key=lambda file_id: nodes[file_id].path))
        for name, ids in sorted(module_buckets.items())
    }
    top_frozen = {
        key: tuple(sorted(ids, key=lambda symbol_id: symbols[symbol_id].source.start_line or 0))
        for key, ids in top_level.items()
    }
    return _Catalogs(
        nodes=nodes,
        by_module=by_module,
        top_level=top_frozen,
        symbols=symbols,
        records=by_id,
    )


build_catalogs = _build_catalogs


def _package_of(node: ModuleNode) -> str | None:
    if not _is_identifier_module(node.module_name) and node.module_name != "":
        return None
    if _is_package_init(node.path):
        return node.module_name
    if not node.module_name:
        return None
    if "." not in node.module_name:
        return ""
    return node.module_name.rpartition(".")[0]


def _resolve_relative_base(node: ModuleNode, level: int, module: str | None) -> str | None:
    package = _package_of(node)
    if package is None:
        return None
    parts = [] if package == "" else package.split(".")
    drop = level - 1
    if drop < 0 or drop > len(parts):
        return None
    if drop:
        parts = parts[:-drop]
    if module:
        parts.extend(module.split("."))
    return ".".join(parts)


def _parent_blocker(
    module: str, catalogs: _Catalogs
) -> tuple[str, str, tuple[ResolutionCandidate, ...]] | None:
    """Return (status, reason, candidates) when a proper prefix blocks descent."""

    for prefix in _proper_prefixes(module):
        ids = catalogs.by_module.get(prefix, ())
        if not ids:
            continue
        if len(ids) > 1:
            candidates = tuple(
                ResolutionCandidate(
                    file_id=file_id,
                    path=catalogs.nodes[file_id].path,
                    module_name=prefix,
                )
                for file_id in ids
            )
            return "ambiguous", "ambiguous_parent", candidates
        node = catalogs.nodes[ids[0]]
        if _is_package_init(node.path):
            continue
        return (
            "unresolved",
            "non_package_parent",
            (
                ResolutionCandidate(
                    file_id=node.file_id,
                    path=node.path,
                    module_name=prefix,
                ),
            ),
        )
    return None


def _candidates_for(module: str, catalogs: _Catalogs) -> tuple[ResolutionCandidate, ...]:
    return tuple(
        ResolutionCandidate(
            file_id=file_id,
            path=catalogs.nodes[file_id].path,
            module_name=module,
        )
        for file_id in catalogs.by_module.get(module, ())
    )


def _relation(
    *,
    importer_id: str,
    source: SourceRef,
    statement_ordinal: int,
    name_ordinal: int,
    spelling: str,
    alias: str | None,
    status: str,
    reason: str | None,
    target_id: str | None = None,
    symbol_id: str | None = None,
    candidates: tuple[ResolutionCandidate, ...] = (),
) -> ImportRelation:
    return ImportRelation(
        importer_id=importer_id,
        source=source,
        statement_ordinal=statement_ordinal,
        name_ordinal=name_ordinal,
        spelling=spelling,
        alias=alias,
        status=status,
        reason=reason,
        target_id=target_id,
        symbol_id=symbol_id,
        candidates=candidates,
    )


def _resolve_exact_module(
    *,
    importer_id: str,
    source: SourceRef,
    statement_ordinal: int,
    name_ordinal: int,
    spelling: str,
    alias: str | None,
    module: str,
    catalogs: _Catalogs,
) -> ImportRelation:
    blocker = _parent_blocker(module, catalogs)
    if blocker is not None:
        status, reason, candidates = blocker
        return _relation(
            importer_id=importer_id,
            source=source,
            statement_ordinal=statement_ordinal,
            name_ordinal=name_ordinal,
            spelling=spelling,
            alias=alias,
            status=status,
            reason=reason,
            candidates=candidates,
        )
    candidates = _candidates_for(module, catalogs)
    if len(candidates) == 1:
        return _relation(
            importer_id=importer_id,
            source=source,
            statement_ordinal=statement_ordinal,
            name_ordinal=name_ordinal,
            spelling=spelling,
            alias=alias,
            status="resolved",
            reason=None,
            target_id=candidates[0].file_id,
        )
    if len(candidates) > 1:
        return _relation(
            importer_id=importer_id,
            source=source,
            statement_ordinal=statement_ordinal,
            name_ordinal=name_ordinal,
            spelling=spelling,
            alias=alias,
            status="ambiguous",
            reason="ambiguous_module",
            candidates=candidates,
        )
    return _relation(
        importer_id=importer_id,
        source=source,
        statement_ordinal=statement_ordinal,
        name_ordinal=name_ordinal,
        spelling=spelling,
        alias=alias,
        status="unresolved",
        reason="not_indexed",
    )


def _unique_ordinary_module(module: str, catalogs: _Catalogs) -> ModuleNode | None:
    ids = catalogs.by_module.get(module, ())
    if len(ids) != 1:
        return None
    node = catalogs.nodes[ids[0]]
    if _is_package_init(node.path):
        return None
    return node


def _resolve_from_item(
    *,
    importer_id: str,
    source: SourceRef,
    statement_ordinal: int,
    name_ordinal: int,
    spelling: str,
    alias: str | None,
    base: str,
    item: str,
    catalogs: _Catalogs,
) -> ImportRelation:
    blocker = _parent_blocker(base, catalogs)
    if blocker is not None:
        status, reason, candidates = blocker
        return _relation(
            importer_id=importer_id,
            source=source,
            statement_ordinal=statement_ordinal,
            name_ordinal=name_ordinal,
            spelling=spelling,
            alias=alias,
            status=status,
            reason=reason,
            candidates=candidates,
        )

    ordinary = _unique_ordinary_module(base, catalogs)
    if ordinary is not None:
        defs = catalogs.top_level.get((ordinary.file_id, item), ())
        if len(defs) == 1:
            return _relation(
                importer_id=importer_id,
                source=source,
                statement_ordinal=statement_ordinal,
                name_ordinal=name_ordinal,
                spelling=spelling,
                alias=alias,
                status="resolved",
                reason=None,
                target_id=ordinary.file_id,
                symbol_id=defs[0],
            )
        if len(defs) > 1:
            return _relation(
                importer_id=importer_id,
                source=source,
                statement_ordinal=statement_ordinal,
                name_ordinal=name_ordinal,
                spelling=spelling,
                alias=alias,
                status="ambiguous",
                reason="ambiguous_member",
                target_id=ordinary.file_id,
                candidates=_candidates_for(base, catalogs),
            )
        return _relation(
            importer_id=importer_id,
            source=source,
            statement_ordinal=statement_ordinal,
            name_ordinal=name_ordinal,
            spelling=spelling,
            alias=alias,
            status="unresolved",
            reason="unresolved_member",
            target_id=ordinary.file_id,
        )

    base_candidates = _candidates_for(base, catalogs)
    child_name = f"{base}.{item}" if base else item
    # Child is eligible only when parents of the child (including base) permit descent.
    child_blocker = _parent_blocker(child_name, catalogs)
    child_candidates: tuple[ResolutionCandidate, ...] = ()
    if child_blocker is None:
        child_candidates = _candidates_for(child_name, catalogs)

    definition_ids: list[str] = []
    if len(base_candidates) == 1:
        definition_ids = list(
            catalogs.top_level.get((base_candidates[0].file_id, item), ())
        )

    if len(base_candidates) > 1:
        return _relation(
            importer_id=importer_id,
            source=source,
            statement_ordinal=statement_ordinal,
            name_ordinal=name_ordinal,
            spelling=spelling,
            alias=alias,
            status="ambiguous",
            reason="ambiguous_module",
            candidates=base_candidates,
        )

    if len(child_candidates) > 1:
        return _relation(
            importer_id=importer_id,
            source=source,
            statement_ordinal=statement_ordinal,
            name_ordinal=name_ordinal,
            spelling=spelling,
            alias=alias,
            status="ambiguous",
            reason="ambiguous_module",
            candidates=child_candidates,
        )

    if len(definition_ids) > 1:
        return _relation(
            importer_id=importer_id,
            source=source,
            statement_ordinal=statement_ordinal,
            name_ordinal=name_ordinal,
            spelling=spelling,
            alias=alias,
            status="ambiguous",
            reason="ambiguous_member",
            target_id=base_candidates[0].file_id if base_candidates else None,
            candidates=base_candidates,
        )

    if len(child_candidates) == 1 and definition_ids:
        return _relation(
            importer_id=importer_id,
            source=source,
            statement_ordinal=statement_ordinal,
            name_ordinal=name_ordinal,
            spelling=spelling,
            alias=alias,
            status="ambiguous",
            reason="ambiguous_child_or_member",
            candidates=child_candidates + base_candidates,
        )

    if len(child_candidates) == 1 and not definition_ids:
        return _relation(
            importer_id=importer_id,
            source=source,
            statement_ordinal=statement_ordinal,
            name_ordinal=name_ordinal,
            spelling=spelling,
            alias=alias,
            status="resolved",
            reason=None,
            target_id=child_candidates[0].file_id,
        )

    if len(definition_ids) == 1 and not child_candidates:
        return _relation(
            importer_id=importer_id,
            source=source,
            statement_ordinal=statement_ordinal,
            name_ordinal=name_ordinal,
            spelling=spelling,
            alias=alias,
            status="resolved",
            reason=None,
            target_id=base_candidates[0].file_id,
            symbol_id=definition_ids[0],
        )

    if len(base_candidates) == 1:
        return _relation(
            importer_id=importer_id,
            source=source,
            statement_ordinal=statement_ordinal,
            name_ordinal=name_ordinal,
            spelling=spelling,
            alias=alias,
            status="unresolved",
            reason="unresolved_member",
            target_id=base_candidates[0].file_id,
        )

    return _relation(
        importer_id=importer_id,
        source=source,
        statement_ordinal=statement_ordinal,
        name_ordinal=name_ordinal,
        spelling=spelling,
        alias=alias,
        status="unresolved",
        reason="not_indexed",
    )


def _resolve_from_wildcard(
    *,
    importer_id: str,
    source: SourceRef,
    statement_ordinal: int,
    name_ordinal: int,
    base: str,
    catalogs: _Catalogs,
) -> ImportRelation:
    spelling = f"{base}.*" if base else "*"
    blocker = _parent_blocker(base, catalogs)
    if blocker is not None:
        status, reason, candidates = blocker
        return _relation(
            importer_id=importer_id,
            source=source,
            statement_ordinal=statement_ordinal,
            name_ordinal=name_ordinal,
            spelling=spelling,
            alias=None,
            status=status,
            reason=reason,
            candidates=candidates,
        )
    candidates = _candidates_for(base, catalogs)
    if len(candidates) == 1:
        return _relation(
            importer_id=importer_id,
            source=source,
            statement_ordinal=statement_ordinal,
            name_ordinal=name_ordinal,
            spelling=spelling,
            alias=None,
            status="resolved",
            reason="wildcard_module_only",
            target_id=candidates[0].file_id,
        )
    if len(candidates) > 1:
        return _relation(
            importer_id=importer_id,
            source=source,
            statement_ordinal=statement_ordinal,
            name_ordinal=name_ordinal,
            spelling=spelling,
            alias=None,
            status="ambiguous",
            reason="ambiguous_module",
            candidates=candidates,
        )
    return _relation(
        importer_id=importer_id,
        source=source,
        statement_ordinal=statement_ordinal,
        name_ordinal=name_ordinal,
        spelling=spelling,
        alias=None,
        status="unresolved",
        reason="not_indexed",
    )


def _consulted_keys_for_import(
    *,
    kind: str,
    module: str | None,
    level: int,
    names: tuple[tuple[str, str | None], ...],
    node: ModuleNode,
) -> frozenset[str]:
    keys: set[str] = set()

    def register(name: str) -> None:
        if not name:
            return
        keys.add(name)
        keys.update(_proper_prefixes(name))

    if kind == "import":
        for spelling, _alias in names:
            register(spelling)
        return frozenset(keys)

    base = _resolve_relative_base(node, level, module) if level else (module or "")
    if base is None:
        return frozenset()
    register(base)
    for spelling, _alias in names:
        if spelling == "*":
            continue
        child = f"{base}.{spelling}" if base else spelling
        register(child)
    return frozenset(keys)


consulted_keys_for_import = _consulted_keys_for_import


def _resolve_imports_for(
    node: ModuleNode, record: FileRecord, catalogs: _Catalogs
) -> tuple[ImportRelation, frozenset[str]]:
    if record.parse_status != "ok":
        return (), frozenset()
    relations: list[ImportRelation] = []
    watched: set[str] = set()
    for statement_ordinal, fact in enumerate(record.facts.imports):
        source = SourceRef(
            path=record.path,
            digest=record.digest,
            start_line=fact.start_line,
            end_line=fact.end_line,
        )
        watched.update(
            _consulted_keys_for_import(
                kind=fact.kind,
                module=fact.module,
                level=fact.level,
                names=fact.names,
                node=node,
            )
        )
        if fact.kind == "import":
            for name_ordinal, (spelling, alias) in enumerate(fact.names):
                relations.append(
                    _resolve_exact_module(
                        importer_id=node.file_id,
                        source=source,
                        statement_ordinal=statement_ordinal,
                        name_ordinal=name_ordinal,
                        spelling=spelling,
                        alias=alias,
                        module=spelling,
                        catalogs=catalogs,
                    )
                )
            continue

        if fact.level:
            base = _resolve_relative_base(node, fact.level, fact.module)
            if base is None:
                for name_ordinal, (spelling, alias) in enumerate(fact.names):
                    relations.append(
                        _relation(
                            importer_id=node.file_id,
                            source=source,
                            statement_ordinal=statement_ordinal,
                            name_ordinal=name_ordinal,
                            spelling=spelling,
                            alias=alias,
                            status="unresolved",
                            reason="invalid_relative",
                        )
                    )
                continue
        else:
            base = fact.module or ""

        for name_ordinal, (spelling, alias) in enumerate(fact.names):
            if spelling == "*":
                relations.append(
                    _resolve_from_wildcard(
                        importer_id=node.file_id,
                        source=source,
                        statement_ordinal=statement_ordinal,
                        name_ordinal=name_ordinal,
                        base=base,
                        catalogs=catalogs,
                    )
                )
            else:
                # Normalized spelling retains the absolute base so unresolved
                # from-imports remain traceable (no invented runtime aliases).
                normalized = f"{base}.{spelling}" if base else spelling
                relations.append(
                    _resolve_from_item(
                        importer_id=node.file_id,
                        source=source,
                        statement_ordinal=statement_ordinal,
                        name_ordinal=name_ordinal,
                        spelling=normalized,
                        alias=alias,
                        base=base,
                        item=spelling,
                        catalogs=catalogs,
                    )
                )
    return tuple(relations), frozenset(watched)


resolve_imports_for = _resolve_imports_for


def _sort_relations(
    relations: tuple[ImportRelation, ...], nodes: dict[str, ModuleNode]
) -> tuple[ImportRelation, ...]:
    def key(item: ImportRelation) -> tuple:
        target_path = ""
        if item.target_id is not None and item.target_id in nodes:
            target_path = nodes[item.target_id].path
        return (
            nodes[item.importer_id].path,
            item.source.start_line or 0,
            item.source.end_line or 0,
            item.statement_ordinal,
            item.name_ordinal,
            target_path,
            item.spelling,
        )

    return tuple(sorted(relations, key=key))


def _edge_worthy(relation: ImportRelation) -> bool:
    """Resolved targets and unresolved_member base edges enter adjacency."""

    if relation.target_id is None or relation.status == "ambiguous":
        return False
    if relation.status == "resolved":
        return True
    return relation.status == "unresolved" and relation.reason == "unresolved_member"


def _publish(
    *,
    generation: IndexGeneration,
    catalogs: _Catalogs,
    relations: tuple[ImportRelation, ...],
    watchers: dict[str, frozenset[str]],
) -> DependencyGraph:
    ordered_relations = _sort_relations(relations, catalogs.nodes)
    forward: dict[str, list[str]] = {file_id: [] for file_id in catalogs.nodes}
    reverse: dict[str, list[str]] = {file_id: [] for file_id in catalogs.nodes}
    symbol_refs: dict[str, list[ImportRelation]] = {}
    neighbor_sets: dict[str, set[str]] = {file_id: set() for file_id in catalogs.nodes}

    for relation in ordered_relations:
        if not _edge_worthy(relation):
            continue
        assert relation.target_id is not None
        forward.setdefault(relation.importer_id, []).append(relation.target_id)
        reverse.setdefault(relation.target_id, []).append(relation.importer_id)
        if relation.importer_id != relation.target_id:
            neighbor_sets.setdefault(relation.importer_id, set()).add(relation.target_id)
            neighbor_sets.setdefault(relation.target_id, set()).add(relation.importer_id)
        if relation.symbol_id is not None:
            symbol_refs.setdefault(relation.symbol_id, []).append(relation)

    def unique_ordered(ids: list[str]) -> tuple[str, ...]:
        seen: set[str] = set()
        ordered: list[str] = []
        for file_id in ids:
            if file_id in seen:
                continue
            seen.add(file_id)
            ordered.append(file_id)
        return tuple(ordered)

    nodes = tuple(sorted(catalogs.nodes.values(), key=lambda item: item.path))
    symbols = tuple(
        sorted(
            catalogs.symbols.values(),
            key=lambda item: (
                item.source.path,
                item.source.start_line or 0,
                item.source.end_line or 0,
                item.qualified_name,
                item.occurrence,
            ),
        )
    )
    graph = DependencyGraph(
        contract_version=GRAPH_CONTRACT_VERSION,
        checkout_key=generation.header.checkout_key,
        policy_key=generation.header.policy_key,
        extractor_key=generation.header.extractor_key,
        schema_version=generation.header.schema_version,
        revision=generation.header.revision,
        manifest_sha256=generation.header.manifest_sha256,
        nodes=nodes,
        symbols=symbols,
        relations=ordered_relations,
        forward={
            file_id: unique_ordered(forward.get(file_id, []))
            for file_id in catalogs.nodes
        },
        reverse={
            file_id: unique_ordered(reverse.get(file_id, []))
            for file_id in catalogs.nodes
        },
        symbol_references={
            symbol_id: tuple(items) for symbol_id, items in sorted(symbol_refs.items())
        },
        neighbors={
            file_id: tuple(
                sorted(
                    neighbor_sets.get(file_id, set()),
                    key=lambda item: catalogs.nodes[item].path,
                )
            )
            for file_id in catalogs.nodes
        },
    )
    object.__setattr__(graph, "_watchers", dict(watchers))
    object.__setattr__(graph, "_records", dict(catalogs.records))
    status_counts: dict[str, int] = {}
    for relation in ordered_relations:
        key = (
            relation.status
            if relation.reason is None
            else f"{relation.status}/{relation.reason}"
        )
        status_counts[key] = status_counts.get(key, 0) + 1
    log.debug(
        "[RepositoryMap.graph] nodes=%s symbols=%s relations=%s statuses=%s",
        len(nodes),
        len(symbols),
        len(ordered_relations),
        status_counts,
    )
    return graph


def build_graph(generation: IndexGeneration, *, resolver=None) -> DependencyGraph:
    """Derive a full static dependency graph from one checked generation."""

    catalogs = _build_catalogs(generation.records)
    relations: list[ImportRelation] = []
    watchers: dict[str, frozenset[str]] = {}
    for file_id, node in catalogs.nodes.items():
        record = catalogs.records[file_id]
        value = None
        if resolver is not None:
            value = resolver(node, record, catalogs)
        if value is not None:
            resolved, watched = value
        else:
            resolved, watched = _resolve_imports_for(node, record, catalogs)
        relations.extend(resolved)
        watchers[file_id] = watched
    return _publish(
        generation=generation,
        catalogs=catalogs,
        relations=tuple(relations),
        watchers=watchers,
    )


def refresh_graph(
    previous: DependencyGraph | None,
    generation: IndexGeneration,
    *,
    resolver=None,
) -> DependencyGraph:
    """Reuse unaffected importer relations when catalogs remain compatible."""

    if previous is None:
        log.debug("[RepositoryMap.refresh] mode=cold")
        return build_graph(generation, resolver=resolver)

    compatible = (
        previous.contract_version == GRAPH_CONTRACT_VERSION
        and previous.checkout_key == generation.header.checkout_key
        and previous.policy_key == generation.header.policy_key
        and previous.extractor_key == generation.header.extractor_key
        and previous.schema_version == generation.header.schema_version
    )
    if not compatible:
        log.debug("[RepositoryMap.refresh] mode=rebuild reason=incompatible")
        return build_graph(generation, resolver=resolver)

    if (
        previous.revision == generation.header.revision
        and previous.manifest_sha256 == generation.header.manifest_sha256
    ):
        log.debug("[RepositoryMap.refresh] mode=hit")
        return previous

    old_records: dict[str, FileRecord] = getattr(previous, "_records", {})
    old_watchers: dict[str, frozenset[str]] = getattr(previous, "_watchers", {})
    catalogs = _build_catalogs(generation.records)
    new_records = catalogs.records

    changed_ids: set[str] = set()
    removed_ids = set(old_records) - set(new_records)
    added_ids = set(new_records) - set(old_records)
    for file_id in set(old_records) & set(new_records):
        if old_records[file_id] != new_records[file_id]:
            changed_ids.add(file_id)

    # Module keys whose ordered bucket membership changed.
    old_catalogs = _build_catalogs(tuple(old_records.values()))
    changed_keys: set[str] = set()
    all_names = set(old_catalogs.by_module) | set(catalogs.by_module)
    for name in all_names:
        old_bucket = [
            (file_id, old_catalogs.records[file_id])
            for file_id in old_catalogs.by_module.get(name, ())
        ]
        new_bucket = [
            (file_id, catalogs.records[file_id])
            for file_id in catalogs.by_module.get(name, ())
        ]
        if old_bucket != new_bucket:
            changed_keys.add(name)

    affected = set(added_ids) | set(changed_ids)
    for file_id, watched in old_watchers.items():
        if file_id in removed_ids:
            continue
        if watched & changed_keys:
            affected.add(file_id)
    affected -= removed_ids

    # Reuse unaffected relations.
    reused = [
        relation
        for relation in previous.relations
        if relation.importer_id not in affected and relation.importer_id not in removed_ids
    ]
    watchers = {
        file_id: watched
        for file_id, watched in old_watchers.items()
        if file_id not in affected and file_id not in removed_ids
    }
    for file_id in affected:
        node = catalogs.nodes[file_id]
        record = catalogs.records[file_id]
        value = None
        if resolver is not None:
            value = resolver(node, record, catalogs)
        if value is not None:
            resolved, watched = value
        else:
            resolved, watched = _resolve_imports_for(node, record, catalogs)
        reused.extend(resolved)
        watchers[file_id] = watched

    log.debug(
        "[RepositoryMap.refresh] mode=delta affected=%s removed=%s reused=%s",
        len(affected),
        len(removed_ids),
        len(reused) - sum(1 for item in reused if item.importer_id in affected),
    )
    return _publish(
        generation=generation,
        catalogs=catalogs,
        relations=tuple(reused),
        watchers=watchers,
    )
