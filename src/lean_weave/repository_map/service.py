"""Reusable RepositoryMap queries over a checked CodeIndex generation."""

from __future__ import annotations

import keyword
from pathlib import PurePosixPath

from lean_weave.code_index import CodeIndex
from lean_weave.context.policy import contains_secret, safe_root
from lean_weave.context.repository import Repository
from lean_weave.errors import ForbiddenContext, RepositoryMapError
from lean_weave.logging_setup import get_logger
from lean_weave.repository_map.graph import refresh_graph
from lean_weave.repository_map.models import (
    DependencyGraph,
    MapLimits,
    MapResult,
    MapSymbol,
    ModuleNode,
)
from lean_weave.repository_map.render import (
    apply_budget,
    definition_row,
    dependency_row,
    escape_map_field,
    module_row,
    reference_row,
)

log = get_logger("repository_map")

_MAX_SELECTOR_BYTES = 512
_CLASS_KINDS = frozenset({"class"})
_MODULE_DEF_KINDS = frozenset({"class", "function", "async_function"})
_METHOD_KINDS = frozenset({"method", "async_method"})


def _validate_selector(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RepositoryMapError("invalid_selector")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise RepositoryMapError("invalid_selector") from None
    if not 1 <= len(encoded) <= _MAX_SELECTOR_BYTES:
        raise RepositoryMapError("invalid_selector")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise RepositoryMapError("invalid_selector")
    if contains_secret(value):
        raise RepositoryMapError("invalid_selector")
    parts = value.split(".")
    if not parts or any(not part.isidentifier() or keyword.iskeyword(part) for part in parts):
        raise RepositoryMapError("invalid_selector")
    return value


def _is_test_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if "tests" in parts:
        return True
    stem = PurePosixPath(path).stem
    return stem.startswith("test_") or stem.endswith("_test")


def _public_components(name: str) -> bool:
    if not name:
        return True
    return all(not part.startswith("_") for part in name.split("."))


class RepositoryMap:
    """Sequential map service over one CodeIndex; graph memo is private."""

    def __init__(self, index: CodeIndex, *, limits: MapLimits | None = None) -> None:
        self._index = index
        self._limits = limits if limits is not None else MapLimits()
        self._graph: DependencyGraph | None = None

    def _load(self) -> DependencyGraph:
        generation = self._index.read_generation()
        self._graph = refresh_graph(self._graph, generation)
        return self._graph

    def show(self) -> MapResult:
        graph = self._load()
        return self._render_module_symbol_view(graph, scope=None, selected=None)

    def module(self, selector: str) -> MapResult:
        name = _validate_selector(selector)
        graph = self._load()
        node = self._unique_module(graph, name)
        neighbor_ids = set(graph.neighbors.get(node.file_id, ()))
        scope = {node.file_id, *neighbor_ids}
        return self._render_module_symbol_view(graph, scope=scope, selected=node.file_id)

    def dependencies(self, selector: str) -> MapResult:
        name = _validate_selector(selector)
        graph = self._load()
        node = self._unique_module(graph, name)
        outgoing = [
            item
            for item in graph.relations
            if item.importer_id == node.file_id
        ]
        incoming = [
            item
            for item in graph.relations
            if item.target_id == node.file_id and item.status == "resolved"
        ]

        def sort_key(item):
            source_node = next(n for n in graph.nodes if n.file_id == item.importer_id)
            return (
                source_node.path,
                item.source.start_line or 0,
                item.source.end_line or 0,
                item.statement_ordinal,
                item.name_ordinal,
            )

        outgoing.sort(key=sort_key)
        incoming.sort(key=sort_key)

        by_id = {item.file_id: item for item in graph.nodes}
        candidates: list[tuple[str, str, str | None]] = []
        index_out = 0
        index_in = 0
        # Interleave starting with incoming when both exist.
        while index_in < len(incoming) or index_out < len(outgoing):
            take_in = index_in < len(incoming) and (
                index_out >= len(outgoing) or (index_in <= index_out)
            )
            if take_in and index_in < len(incoming):
                item = incoming[index_in]
                index_in += 1
                direction = "in"
            elif index_out < len(outgoing):
                item = outgoing[index_out]
                index_out += 1
                direction = "out"
            else:
                item = incoming[index_in]
                index_in += 1
                direction = "in"
            if item.target_id is not None and item.target_id in by_id:
                target = by_id[item.target_id].module_name or by_id[item.target_id].path
            elif item.status == "resolved":
                target = "resolved"
            elif item.reason:
                target = f"{item.status}({item.reason})"
            else:
                target = item.status
            row = dependency_row(
                direction=direction,
                path=item.source.path,
                start=item.source.start_line or 0,
                end=item.source.end_line or 0,
                spelling=item.spelling,
                alias=item.alias,
                target=target,
            )
            if row is None:
                continue
            candidates.append((row, item.source.path, None))
        result = apply_budget(candidates, revision=graph.revision, limits=self._limits)
        log.debug(
            "[RepositoryMap.view] view=dependencies rows=%s tokens=%s truncated=%s",
            len(result.rows),
            result.estimated_tokens,
            result.truncated,
        )
        return result

    def references(self, selector: str) -> MapResult:
        name = _validate_selector(selector)
        graph = self._load()
        symbols = self._resolve_symbol_group(graph, name)
        candidates: list[tuple[str, str, str | None]] = []
        definitions = sorted(
            symbols,
            key=lambda item: (
                item.source.path,
                item.source.start_line or 0,
                item.source.end_line or 0,
                item.qualified_name,
            ),
        )
        for symbol in definitions:
            long = definition_row(
                symbol.source.path,
                symbol.source.start_line or 0,
                symbol.source.end_line or 0,
                symbol.qualified_name,
                symbol.signature,
                kind=symbol.kind,
            )
            short = definition_row(
                symbol.source.path,
                symbol.source.start_line or 0,
                symbol.source.end_line or 0,
                symbol.qualified_name,
                symbol.signature,
                kind=symbol.kind,
                short=True,
            )
            if long is None:
                continue
            candidates.append((long, symbol.source.path, short))

        import_sites = []
        for symbol in symbols:
            for relation in graph.symbol_references.get(symbol.symbol_id, ()):
                import_sites.append((symbol, relation))
        import_sites.sort(
            key=lambda pair: (
                pair[1].source.path,
                pair[1].source.start_line or 0,
                pair[1].source.end_line or 0,
                pair[1].statement_ordinal,
                pair[1].name_ordinal,
            )
        )
        for symbol, relation in import_sites:
            detail = f"{relation.spelling}"
            if relation.alias:
                detail = f"{relation.spelling} as {relation.alias}"
            detail = f"{detail} -> {symbol.qualified_name}"
            row = reference_row(
                kind="import",
                path=relation.source.path,
                start=relation.source.start_line or 0,
                end=relation.source.end_line or 0,
                detail=detail,
            )
            if row is None:
                continue
            candidates.append((row, relation.source.path, None))

        # Prefix definition rows with def| — rewrite long/short already emitted.
        rewritten: list[tuple[str, str, str | None]] = []
        definition_count = len(definitions)
        for index, (row, path, short) in enumerate(candidates):
            if index < definition_count:
                prefixed = f"def {row}"
                prefixed_short = None if short is None else f"def {short}"
                rewritten.append((prefixed, path, prefixed_short))
            else:
                rewritten.append((row, path, short))
        result = apply_budget(rewritten, revision=graph.revision, limits=self._limits)
        log.debug(
            "[RepositoryMap.view] view=references rows=%s tokens=%s truncated=%s",
            len(result.rows),
            result.estimated_tokens,
            result.truncated,
        )
        return result

    def for_context(
        self,
        *,
        allowed_paths: frozenset[str] | None,
        repository: Repository,
    ) -> MapResult:
        """Map-only context projection; module/symbol rows, no edge diagnostics."""

        index_root = safe_root(self._index.root)
        if index_root != repository.root:
            raise ForbiddenContext("Repository map checkout does not match the context repository")
        index_repo = self._index._repository()
        if (
            index_repo.state_dir != repository.state_dir
            or tuple(sorted(index_repo.protected_paths))
            != tuple(sorted(repository.protected_paths))
            or list(self._index.context_config.exclusions)
            != list(repository.config.exclusions)
            or self._index.context_config.candidate_bytes_max
            != repository.config.candidate_bytes_max
            or self._index.context_config.scan_bytes_max != repository.config.scan_bytes_max
            or self._index.context_config.visited_entries_max
            != repository.config.visited_entries_max
        ):
            raise ForbiddenContext("Repository map admission policy does not match the context repository")

        graph = self._load()
        if allowed_paths is not None:
            present = {node.path for node in graph.nodes}
            if allowed_paths and any(path not in present for path in allowed_paths):
                raise RepositoryMapError("not_found")
            scope_ids = {
                node.file_id for node in graph.nodes if node.path in allowed_paths
            }
        else:
            scope_ids = None
        return self._render_module_symbol_view(
            graph,
            scope=scope_ids,
            selected=None,
            include_tests=allowed_paths is not None,
        )

    def _unique_module(self, graph: DependencyGraph, name: str) -> ModuleNode:
        matches = [node for node in graph.nodes if node.module_name == name]
        if not matches:
            raise RepositoryMapError("not_found")
        if len(matches) > 1:
            raise RepositoryMapError("ambiguous_selector")
        return matches[0]

    def _local_name(self, graph: DependencyGraph, symbol: MapSymbol) -> str:
        node = next(item for item in graph.nodes if item.file_id == symbol.file_id)
        if node.module_name and symbol.qualified_name.startswith(node.module_name + "."):
            return symbol.qualified_name[len(node.module_name) + 1 :]
        return symbol.qualified_name

    def _resolve_symbol_group(self, graph: DependencyGraph, name: str) -> tuple[MapSymbol, ...]:
        dotted = "." in name
        groups: dict[tuple[str, str, str], list[MapSymbol]] = {}
        for symbol in graph.symbols:
            if dotted:
                if symbol.qualified_name != name:
                    continue
            elif symbol.simple_name != name:
                continue
            identity = (symbol.file_id, self._local_name(graph, symbol), symbol.kind)
            groups.setdefault(identity, []).append(symbol)
        if not groups:
            raise RepositoryMapError("not_found")
        if len(groups) > 1:
            raise RepositoryMapError("ambiguous_selector")
        return tuple(next(iter(groups.values())))

    def _visible_symbols(
        self, graph: DependencyGraph, file_id: str
    ) -> list[MapSymbol]:
        by_local: dict[str, MapSymbol] = {}
        for symbol in graph.symbols:
            if symbol.file_id != file_id:
                continue
            node = next(n for n in graph.nodes if n.file_id == file_id)
            local_name = (
                symbol.qualified_name[len(node.module_name) + 1 :]
                if node.module_name and symbol.qualified_name.startswith(node.module_name + ".")
                else symbol.qualified_name
            )
            by_local[local_name] = symbol

        visible: list[MapSymbol] = []
        for symbol in graph.symbols:
            if symbol.file_id != file_id:
                continue
            if not _public_components(symbol.simple_name):
                continue
            if symbol.parent is None:
                if symbol.kind in _MODULE_DEF_KINDS:
                    visible.append(symbol)
                continue
            if symbol.kind not in _METHOD_KINDS and symbol.kind not in _CLASS_KINDS:
                continue
            # Class-only ancestors.
            ok = True
            cursor = symbol.parent
            while cursor:
                ancestor = by_local.get(cursor)
                if ancestor is None or ancestor.kind not in _CLASS_KINDS:
                    ok = False
                    break
                cursor = ancestor.parent
            if ok and symbol.kind in _METHOD_KINDS | _CLASS_KINDS:
                if symbol.kind in _CLASS_KINDS and symbol.parent is not None:
                    # Nested class through class ancestors — public if name public.
                    visible.append(symbol)
                elif symbol.kind in _METHOD_KINDS:
                    visible.append(symbol)
        return visible

    def _rank_modules(
        self,
        graph: DependencyGraph,
        *,
        scope: set[str] | None,
        include_tests: bool,
    ) -> list[ModuleNode]:
        filtered: list[ModuleNode] = []
        for node in graph.nodes:
            if scope is not None and node.file_id not in scope:
                continue
            if not include_tests and _is_test_path(node.path):
                continue
            if scope is None and not _public_components(node.module_name):
                continue
            publics = self._visible_symbols(graph, node.file_id)
            has_edge = any(
                dst != node.file_id for dst in graph.forward.get(node.file_id, ())
            ) or any(src != node.file_id for src in graph.reverse.get(node.file_id, ()))
            if scope is None and not publics and not has_edge:
                continue
            filtered.append(node)

        def sort_key(node: ModuleNode):
            incoming = len(
                {src for src in graph.reverse.get(node.file_id, ()) if src != node.file_id}
            )
            outgoing = len(
                {dst for dst in graph.forward.get(node.file_id, ()) if dst != node.file_id}
            )
            return (-incoming, -outgoing, node.module_name, node.path)

        filtered.sort(key=sort_key)
        return filtered

    def _symbol_importer_count(self, graph: DependencyGraph, symbol: MapSymbol) -> int:
        return len(
            {
                relation.importer_id
                for relation in graph.symbol_references.get(symbol.symbol_id, ())
            }
        )

    def _render_module_symbol_view(
        self,
        graph: DependencyGraph,
        *,
        scope: set[str] | None,
        selected: str | None,
        include_tests: bool = False,
    ) -> MapResult:
        modules = self._rank_modules(graph, scope=scope, include_tests=include_tests)
        if selected is not None:
            selected_node = next(n for n in graph.nodes if n.file_id == selected)
            modules = [selected_node] + [m for m in modules if m.file_id != selected]

        # Dedup definitions by (file_id, local_name, kind, normalized signature)
        def dedupe(symbols: list[MapSymbol]) -> list[MapSymbol]:
            best: dict[tuple, MapSymbol] = {}
            for symbol in symbols:
                node = next(n for n in graph.nodes if n.file_id == symbol.file_id)
                local_name = (
                    symbol.qualified_name[len(node.module_name) + 1 :]
                    if node.module_name and symbol.qualified_name.startswith(node.module_name + ".")
                    else symbol.qualified_name
                )
                key = (symbol.file_id, local_name, symbol.kind, symbol.signature or "")
                existing = best.get(key)
                if existing is None or (symbol.source.start_line or 0) < (
                    existing.source.start_line or 0
                ):
                    best[key] = symbol
            return list(best.values())

        kind_rank = {"class": 0, "function": 1, "async_function": 2, "method": 3, "async_method": 4}
        per_module_symbols: dict[str, list[MapSymbol]] = {}
        for node in modules:
            symbols = dedupe(self._visible_symbols(graph, node.file_id))
            symbols.sort(
                key=lambda item: (
                    -self._symbol_importer_count(graph, item),
                    kind_rank.get(item.kind, 9),
                    item.qualified_name,
                    item.source.start_line or 0,
                    item.source.end_line or 0,
                )
            )
            per_module_symbols[node.file_id] = symbols

        candidates: list[tuple[str, str, str | None]] = []
        seed_truncated = False

        def add_definition(symbol: MapSymbol) -> None:
            nonlocal seed_truncated
            short = definition_row(
                symbol.source.path,
                symbol.source.start_line or 0,
                symbol.source.end_line or 0,
                symbol.qualified_name,
                symbol.signature,
                kind=symbol.kind,
                short=True,
            )
            signature = symbol.signature or ""
            long_too_big = len(escape_map_field(signature).encode("utf-8")) > 160
            if long_too_big:
                seed_truncated = True
                if short is not None:
                    candidates.append((short, symbol.source.path, None))
                return
            long = definition_row(
                symbol.source.path,
                symbol.source.start_line or 0,
                symbol.source.end_line or 0,
                symbol.qualified_name,
                symbol.signature,
                kind=symbol.kind,
            )
            if long is not None:
                candidates.append((long, symbol.source.path, short))

        # Header + best symbol per module, then remaining symbols.
        remaining: list[tuple[ModuleNode, MapSymbol]] = []
        for node in modules:
            header = module_row(node.module_name, node.path)
            if header is not None:
                candidates.append((header, node.path, None))
            symbols = per_module_symbols.get(node.file_id, [])
            if symbols:
                best, *rest = symbols
                add_definition(best)
                for item in rest:
                    remaining.append((node, item))

        remaining.sort(
            key=lambda pair: (
                next(
                    i for i, mod in enumerate(modules) if mod.file_id == pair[0].file_id
                ),
                -self._symbol_importer_count(graph, pair[1]),
                kind_rank.get(pair[1].kind, 9),
                pair[1].qualified_name,
                pair[1].source.start_line or 0,
            )
        )
        for _node, symbol in remaining:
            add_definition(symbol)

        result = apply_budget(
            candidates,
            revision=graph.revision,
            limits=self._limits,
            seed_truncated=seed_truncated,
        )
        log.debug(
            "[RepositoryMap.view] view=map rows=%s tokens=%s truncated=%s",
            len(result.rows),
            result.estimated_tokens,
            result.truncated,
        )
        return result
