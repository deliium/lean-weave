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
    wrap(ContextPacker, "build", "pack_build")
    wrap(SqliteContextCache, "_lookup", "cache_lookup")
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
    iterations: int = ITERATIONS,
) -> dict:
    setup_start = time.perf_counter()
    cache = _cache(root, enabled=enabled, base=base)
    if warm and cache is not None:
        # Prime outside timing.
        _measure_once(root, case, cache=cache, indexed=indexed, base=base)
    setup_ms = round((time.perf_counter() - setup_start) * 1000, 3)
    signatures: list[str] = []
    times: list[float] = []
    cpu: list[float] = []
    counters = _Counters()
    stats = None
    db_bytes = 0
    for _ in range(iterations):
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
    kind_events: dict[str, dict[str, int]] = {}
    if cache is not None:
        status = cache.status()
        stats = cache.stats()
        db_bytes = status.db_bytes or 0
        kind_events = {
            str(kind): dict(events) for kind, events in stats.kind_events
        }
    return {
        "enabled": enabled,
        "warm": warm,
        "signature": signatures[0] if signatures else "",
        "signatures_equal": len(set(signatures)) == 1,
        "wall_ms_median": round(statistics.median(times), 3),
        "cpu_ms_median": round(statistics.median(cpu), 3),
        "wall_ms_samples": [round(value, 3) for value in times],
        "cpu_ms_samples": [round(value, 3) for value in cpu],
        "setup_ms": setup_ms,
        "calls": dict(sorted(counters.values.items())),
        "db_bytes": db_bytes,
        "hits": 0 if stats is None else stats.count("hit"),
        "misses": 0 if stats is None else stats.count("miss"),
        "hits_by_kind": {
            kind: events.get("hit", 0) for kind, events in sorted(kind_events.items())
        },
        "misses_by_kind": {
            kind: events.get("miss", 0) for kind, events in sorted(kind_events.items())
        },
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


def measure_index_updates(scratch: Path) -> dict:
    """Time initial build, unchanged update, edit, rename, delete and clean rebuild.

    Operates on a private copied checkout in ``scratch`` and restores every edit
    so subsequent measurements see the pinned content.
    """

    cases = {case.id: case for case in load_cases()}
    case = cases["bug_fix_add"]
    root = materialize_repository(case.repository, scratch / "index" / "repo")
    base = scratch / "index" / "cache"
    base.mkdir(parents=True, mode=0o700)
    target = root / "src" / "calc.py"
    original = target.read_bytes()

    def timed(operation):
        start = time.perf_counter()
        update = operation()
        return round((time.perf_counter() - start) * 1000, 3), update

    index = CodeIndex(root, context_config=ContextConfig(), cache_base=base)
    build_ms, build_update = timed(index.build)
    unchanged_ms, unchanged_update = timed(index.update)
    target.write_bytes(original + b"\n# benchmark edit\n")
    edit_ms, edit_update = timed(index.update)
    target.write_bytes(original)
    restore_ms, restore_update = timed(index.update)
    renamed = target.with_name("calc_renamed.py")
    target.rename(renamed)
    rename_ms, rename_update = timed(index.update)
    renamed.rename(target)
    target.unlink()
    delete_ms, delete_update = timed(index.update)
    target.write_bytes(original)
    recreate_ms, recreate_update = timed(index.update)
    fresh_cache = scratch / "index" / "clean-cache"
    fresh_cache.mkdir(parents=True, mode=0o700)
    clean_index = CodeIndex(
        root, context_config=ContextConfig(), cache_base=fresh_cache
    )
    clean_ms, clean_update = timed(clean_index.build)
    status = index.status()
    clean_status = clean_index.status()
    return {
        "case_id": case.id,
        "build_ms": build_ms,
        "unchanged_update_ms": unchanged_ms,
        "edit_update_ms": edit_ms,
        "restore_update_ms": restore_ms,
        "rename_update_ms": rename_ms,
        "delete_update_ms": delete_ms,
        "recreate_update_ms": recreate_ms,
        "clean_rebuild_ms": clean_ms,
        "counts": {
            "build": dict(build_update.counts),
            "unchanged": dict(unchanged_update.counts),
            "edit": dict(edit_update.counts),
            "rename": dict(rename_update.counts),
            "delete": dict(delete_update.counts),
            "clean": dict(clean_update.counts),
        },
        "status": {
            "files": status.files,
            "symbols": status.symbols,
            "imports": status.imports,
        },
        "clean_status": {
            "files": clean_status.files,
            "symbols": clean_status.symbols,
            "imports": clean_status.imports,
        },
        "incremental_matches_clean": (
            status.files == clean_status.files
            and status.symbols == clean_status.symbols
            and status.imports == clean_status.imports
        ),
        "restored": target.read_bytes() == original,
    }


@dataclass(frozen=True)
class SyntheticCase:
    """Bounded synthetic workload label; never a product fixture."""

    id: str
    query: str
    task_kind: str = "feature"


def _write_synthetic_tree(
    root: Path, *, offscope_files: int, dense_imports: int = 0
) -> None:
    src = root / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "target.py").write_text(
        "def alpha(value):\n    return value\n\n\ndef beta():\n    return alpha(1)\n",
        encoding="utf-8",
    )
    for index in range(offscope_files):
        path = root / "offscope" / f"mod_{index:04d}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"def noise_{index}():\n    return {index}\n", encoding="utf-8"
        )
    for index in range(dense_imports):
        path = root / "dense" / f"d{index:03d}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "from src.target import alpha\n\n\n"
            f"def use_{index}():\n    return alpha({index})\n",
            encoding="utf-8",
        )


def run_scaled_benchmark(
    *,
    scratch: Path | None = None,
    sizes: tuple[int, ...] = (100, 1000),
    dense_imports: int = 50,
    iterations: int = 2,
) -> dict:
    """Measure off-scope fanout and one dense-import tree.

    Synthetic trees are explicitly labeled. Scope remains the target module;
    only the number of unrelated files or importers grows.
    """

    base_root = scratch or Path(tempfile.mkdtemp(prefix="lw-cache-scaled-"))
    results: dict[str, dict] = {}
    for size in sizes:
        root = base_root / f"scaled_{size}" / "repo"
        root.mkdir(parents=True, exist_ok=True)
        _write_synthetic_tree(root, offscope_files=size)
        base = base_root / f"scaled_{size}" / "cache"
        base.mkdir(parents=True, mode=0o700, exist_ok=True)
        case = SyntheticCase(id=f"scaled_{size}", query="alpha target")
        mode = _run_mode(
            root,
            case,
            enabled=False,
            warm=False,
            base=base,
            indexed=False,
            iterations=iterations,
        )
        results[f"scaled_{size}"] = {
            "synthetic": True,
            "offscope_files": size,
            "dense_imports": 0,
            "modes": [mode],
        }
    root = base_root / "dense_imports" / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _write_synthetic_tree(root, offscope_files=0, dense_imports=dense_imports)
    base = base_root / "dense_imports" / "cache"
    base.mkdir(parents=True, mode=0o700, exist_ok=True)
    case = SyntheticCase(id="dense_imports", query="alpha")
    mode = _run_mode(
        root,
        case,
        enabled=False,
        warm=False,
        base=base,
        indexed=True,
        iterations=iterations,
    )
    results["dense_imports"] = {
        "synthetic": True,
        "offscope_files": 0,
        "dense_imports": dense_imports,
        "modes": [mode],
    }
    return results


def run_benchmark(*, scratch: Path | None = None) -> dict:
    cases = {case.id: case for case in load_cases()}
    selected = [cases[name] for name in CASES if name in cases]
    base_root = scratch or Path(tempfile.mkdtemp(prefix="lw-cache-bench-"))
    index_updates = measure_index_updates(base_root / "index-timing")
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
            output[f"{case.id}/{workload}"] = {
                "modes": modes,
                "index_updates": index_updates,
            }
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    parser.add_argument(
        "--scaled", action="store_true", help="Include synthetic 100/1,000-file workloads"
    )
    args = parser.parse_args(argv)
    result = run_benchmark()
    if args.scaled:
        result["scaled_workloads"] = run_scaled_benchmark()
    if args.json:
        print(json.dumps(result, sort_keys=True, indent=2))
    else:
        for name, item in result.items():
            if name == "scaled_workloads":
                continue
            for mode in item["modes"]:
                print(
                    f"{name} enabled={mode['enabled']} warm={mode['warm']} "
                    f"wall_ms={mode['wall_ms_median']} hits={mode['hits']} "
                    f"calls={mode['calls']}"
                )
        for name, item in (result.get("scaled_workloads") or {}).items():
            mode = item["modes"][0]
            print(
                f"{name} synthetic=true offscope={item['offscope_files']} "
                f"dense={item['dense_imports']} wall_ms={mode['wall_ms_median']} "
                f"calls={mode['calls']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
