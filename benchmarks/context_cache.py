"""Offline cold/warm/disabled measurement for the context cache.

This is developer tooling.  It reports local deterministic work separately from
API token estimates and never dials a model.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import statistics
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from lean_weave.code_index import CodeIndex
from lean_weave.config.models import ContextCacheConfig, ContextConfig
from lean_weave.context import packing_source
from lean_weave.context import retriever as retriever_module
from lean_weave.context.packer import ContextPacker
from lean_weave.context.packing_models import PackRequest
from lean_weave.context.python_index import extract_python_facts
from lean_weave.context.repository import Repository
from lean_weave.context.retrieval_models import RetrievalRequest
from lean_weave.context.retriever import ContextRetriever
from lean_weave.context_cache.service import SqliteContextCache
from lean_weave.repository_map import RepositoryMap
from lean_weave.repository_map import graph as graph_module
from lean_weave.repository_map import render

from benchmarks.retrieval import load_cases, materialize_repository

CASES = ("bug_fix_add", "multi_file_banner")
ITERATIONS = 5


@dataclass
class _Counters:
    values: dict[str, int] = field(default_factory=dict)

    def bump(self, name: str) -> None:
        self.values[name] = self.values.get(name, 0) + 1


@contextlib.contextmanager
def _instrument(counters: _Counters):
    """Count representative deterministic calls without changing behavior."""

    patches: list[tuple[object, str, object]] = []

    def wrap(owner, name: str, label: str, *, raise_on_call: bool = False):
        original = getattr(owner, name)

        def counted(*args, **kwargs):
            counters.bump(label)
            return original(*args, **kwargs)

        setattr(owner, name, counted)
        patches.append((owner, name, original))

    wrap(retriever_module, "extract_python_facts", "facts_extract")
    wrap(packing_source, "extract_python_facts", "facts_extract")
    wrap(packing_source, "_python_spans", "spans")
    wrap(retriever_module.ContextRetriever, "_rank_local", "rank_local")
    wrap(graph_module, "resolve_imports_for", "imports")
    wrap(render, "module_row", "map_header")
    wrap(render, "definition_row", "map_definition")
    try:
        yield
    finally:
        for owner, name, original in reversed(patches):
            setattr(owner, name, original)


def _signature(result, pack) -> str:
    payload = {
        "retrieval": result.model_dump(mode="json"),
        "pack": None if pack is None else pack.model_dump(mode="json"),
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cache(root: Path, *, enabled: bool, base: Path) -> SqliteContextCache | None:
    if not enabled:
        return None
    return SqliteContextCache(
        root,
        config=ContextCacheConfig(enabled=True),
        context_config=ContextConfig(),
        cache_base=base,
    )


def _run_mode(
    root: Path,
    case,
    *,
    enabled: bool,
    warm: bool,
    base: Path,
    indexed: bool,
) -> dict:
    cache = _cache(root, enabled=enabled, base=base)
    if warm and cache is not None:
        # Prime outside timing.
        _measure_once(root, case, cache=cache, indexed=indexed, base=base)
    signatures: list[str] = []
    times: list[float] = []
    cpu: list[float] = []
    counters = _Counters()
    stats = None
    db_bytes = 0
    for _ in range(ITERATIONS):
        if enabled and not warm and cache is not None:
            cache.clear()
        start = time.perf_counter()
        cpu_start = time.process_time()
        with _instrument(counters):
            signature = _measure_once(
                root, case, cache=cache, indexed=indexed, base=base
            )
        times.append((time.perf_counter() - start) * 1000)
        cpu.append((time.process_time() - cpu_start) * 1000)
        signatures.append(signature)
    if cache is not None:
        status = cache.status()
        stats = cache.stats()
        db_bytes = status.db_bytes or 0
    return {
        "enabled": enabled,
        "warm": warm,
        "signature": signatures[0] if signatures else "",
        "signatures_equal": len(set(signatures)) == 1,
        "wall_ms_median": round(statistics.median(times), 3),
        "cpu_ms_median": round(statistics.median(cpu), 3),
        "calls": dict(sorted(counters.values.items())),
        "db_bytes": db_bytes,
        "hits": 0 if stats is None else stats.count("hit"),
        "misses": 0 if stats is None else stats.count("miss"),
        "llm_calls": 0,
        "api_tokens_measured": False,
    }


def _measure_once(root: Path, case, *, cache, indexed: bool, base: Path) -> str:
    repository = Repository(root, ContextConfig(), strict_source=True)
    load_graph = None
    if indexed:
        index = CodeIndex(root, context_config=ContextConfig(), cache_base=base)
        map_service = RepositoryMap(index, cache=cache)
        load_graph = map_service.graph_for_retrieval_scan
    retriever = ContextRetriever(
        repository, load_graph=load_graph, cache=cache
    )
    packer = ContextPacker(
        repository,
        retriever=retriever,
        cache=cache,
    )
    request = RetrievalRequest(
        query=case.query, task_kind=case.task_kind, kind="all"
    )
    result = retriever.retrieve(request)
    pack = packer.build(
        PackRequest(
            task_id=case.id,
            query=case.query,
            task_specification=case.query,
            system_instructions="Offline benchmark.",
            level="l1",
        )
    )
    return _signature(result, pack.pack)


def run_benchmark(*, scratch: Path | None = None) -> dict:
    cases = {case.id: case for case in load_cases()}
    selected = [cases[name] for name in CASES if name in cases]
    base_root = scratch or Path(tempfile.mkdtemp(prefix="lw-cache-bench-"))
    output: dict[str, dict] = {}
    for case in selected:
        root = materialize_repository(case.repository, base_root / case.id)
        base = base_root / case.id / "cache"
        base.mkdir(mode=0o700)
        for workload, indexed in (("no_index", False), ("indexed", True)):
            if indexed:
                CodeIndex(
                    root, context_config=ContextConfig(), cache_base=base
                ).build()
            modes = [
                _run_mode(
                    root,
                    case,
                    enabled=False,
                    warm=False,
                    base=base,
                    indexed=indexed,
                ),
                _run_mode(
                    root,
                    case,
                    enabled=True,
                    warm=False,
                    base=base,
                    indexed=indexed,
                ),
                _run_mode(
                    root,
                    case,
                    enabled=True,
                    warm=True,
                    base=base,
                    indexed=indexed,
                ),
            ]
            signatures = {mode["signature"] for mode in modes}
            if len(signatures) != 1:
                raise SystemExit(
                    f"cache mode output mismatch for {case.id}/{workload}"
                )
            output[f"{case.id}/{workload}"] = {"modes": modes}
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args(argv)
    result = run_benchmark()
    if args.json:
        print(json.dumps(result, sort_keys=True, indent=2))
    else:
        for name, item in result.items():
            for mode in item["modes"]:
                print(
                    f"{name} enabled={mode['enabled']} warm={mode['warm']} "
                    f"wall_ms={mode['wall_ms_median']} hits={mode['hits']} "
                    f"calls={mode['calls']}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
