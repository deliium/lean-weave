"""Bounded offline seeding for explicit context-cache rebuilds."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lean_weave.app import (
    AppContext,
    create_code_index,
    create_context_cache,
)
from lean_weave.context.packing_source import _python_spans
from lean_weave.context.policy import is_python
from lean_weave.context.python_index import extract_python_facts
from lean_weave.context.repository import Repository
from lean_weave.context_cache.facts import FactsCodec, facts_key, facts_proof
from lean_weave.context_cache.imports import ImportCache, ImportCodec
from lean_weave.context_cache.map_rows import MapRowCache
from lean_weave.context_cache.models import (
    CacheBatch,
    CacheOutcome,
    CacheResult,
)
from lean_weave.context_cache.packing import SpanCodec, PackingSpanCache
from lean_weave.context_cache.service import CachePolicy, SqliteContextCache
from lean_weave.errors import CodeIndexError
from lean_weave.logging_setup import get_logger
from lean_weave.repository_map.graph import (
    build_catalogs,
    build_graph,
    consulted_keys_for_import,
    resolve_imports_for,
)

log = get_logger("context_cache.maintenance")


@dataclass(frozen=True)
class SeedReport:
    facts: int = 0
    spans: int = 0
    imports: int = 0
    map_seed: str = "skipped"
    capacity_skips: int = 0


class _SeedCache:
    """Adapter that routes adapter publications into one pending writer."""

    enabled = True

    def __init__(self, writer) -> None:
        self.writer = writer
        self.capacity_skips = 0

    def lookup(self, key, proof, *, codec):
        return CacheOutcome(CacheResult.MISS)

    def begin_batch(self):
        return CacheBatch(None, usable=False)

    def publish(self, key, proof, value, *, codec, batch=None) -> bool:
        ok = self.writer.publish(key, proof, value, codec=codec)
        if not ok:
            self.capacity_skips += 1
        return ok


def _seed(
    writer,
    *,
    scan,
    generation,
    policy: CachePolicy,
) -> SeedReport:
    adapter = _SeedCache(writer)
    facts_codec = FactsCodec()
    fact_rows = 0
    span_rows = 0
    for path in sorted(item.path for item in scan.snapshots if is_python(item.path)):
        snapshot = next(item for item in scan.snapshots if item.path == path)
        facts = extract_python_facts(snapshot)
        if adapter.publish(
            facts_key(policy, snapshot),
            facts_proof(policy, snapshot),
            facts,
            codec=facts_codec,
        ):
            fact_rows += 1
        spans = _python_spans(snapshot)
        if adapter.publish(
            PackingSpanCache(adapter, policy=policy).key(snapshot),
            PackingSpanCache(adapter, policy=policy).proof(snapshot),
            spans,
            codec=SpanCodec(snapshot),
        ):
            span_rows += 1

    import_rows = 0
    map_seed = "skipped"
    if generation is None:
        return SeedReport(
            facts=fact_rows,
            spans=span_rows,
            imports=0,
            map_seed=map_seed,
            capacity_skips=adapter.capacity_skips,
        )

    catalogs = build_catalogs(generation.records)
    imports = ImportCache(adapter, policy=policy, header=generation.header)
    for file_id, node in catalogs.nodes.items():
        record = catalogs.records[file_id]
        watched = frozenset()
        for fact in record.facts.imports:
            watched |= consulted_keys_for_import(
                kind=fact.kind,
                module=fact.module,
                level=fact.level,
                names=fact.names,
                node=node,
            )
        value = resolve_imports_for(node, record, catalogs)
        codec = ImportCodec(
            record=record, node=node, catalogs=catalogs, watched=watched
        )
        if adapter.publish(
            imports.key(record, node),
            imports.proof(record, node, catalogs, watched),
            value,
            codec=codec,
        ):
            import_rows += 1

    graph = build_graph(generation)
    rows = MapRowCache(adapter, policy=policy)
    for node in graph.nodes:
        rows.header(node.module_name, node.path, node.digest, batch=None)
    for symbol in graph.symbols:
        rows.definition(symbol, batch=None)
    map_seed = "ready"
    return SeedReport(
        facts=fact_rows,
        spans=span_rows,
        imports=import_rows,
        map_seed=map_seed,
        capacity_skips=adapter.capacity_skips,
    )


def rebuild_context_cache(
    context: AppContext,
    *,
    workspace_root: Path | None = None,
    cache_base: Path | None = None,
) -> tuple[object, SeedReport]:
    """Open one scan, detach an optional checked generation, then rebuild.

    The CodeIndex read lease is released before the cache maintenance lease is
    acquired; no cache seed callback opens the index or another namespace.
    """

    root = Path.cwd() if workspace_root is None else Path(workspace_root)
    cache = create_context_cache(
        context, workspace_root=root, cache_base=cache_base
    )
    if not isinstance(cache, SqliteContextCache):
        raise CodeIndexError("incompatible")
    index = create_code_index(context, workspace_root=root, cache_base=cache_base)
    repository = index._repository()
    policy = CachePolicy.from_context(
        context.config.context,
        state_dir=context.config.workflow.state_dir,
        protected_paths=index.protected_paths,
    )
    with repository.scan_retrieval_sources() as scan:
        if not scan.complete:
            raise CodeIndexError("incomplete")
        generation = None
        try:
            generation = index.read_generation_for_retrieval_scan(scan)
        except CodeIndexError:
            generation = None
        report_holder: list[SeedReport] = []

        def seed(writer):
            report_holder.append(
                _seed(
                    writer,
                    scan=scan,
                    generation=generation,
                    policy=policy,
                )
            )

        status = cache.rebuild(seed, revalidate=scan.revalidate)
        report = report_holder[0] if report_holder else SeedReport()
        log.info(
            "[ContextCache.rebuild] facts=%s spans=%s imports=%s map=%s capacity_skips=%s",
            report.facts,
            report.spans,
            report.imports,
            report.map_seed,
            report.capacity_skips,
        )
        return status, report
