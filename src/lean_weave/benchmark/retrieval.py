"""Offline V1 vs V2 retrieval quality comparison (developer tooling)."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from lean_weave.code_index import CodeIndex
from lean_weave.config.models import ContextConfig
from lean_weave.context.builder import ContextBuilder
from lean_weave.context.git_metadata import read_retrieval_diff
from lean_weave.context.models import ContextQuery, ExpansionLevel
from lean_weave.context.packer import ContextPacker
from lean_weave.context.packing_models import PackEnvelope, PackRequest
from lean_weave.context.python_index import build_python_index
from lean_weave.context.ranking import rank
from lean_weave.context.repository import Repository
from lean_weave.context.retrieval_models import RetrievalRequest
from lean_weave.context.retriever import ContextRetriever
from lean_weave.context_cache import SqliteContextCache
from lean_weave.metrics.budget import estimate_tokens
from lean_weave.repository_map import RepositoryMap
from lean_weave.verify.process import run_bounded

from lean_weave.benchmark.fixtures import (
    COMMIT_MESSAGE,
    FIXED_AUTHOR_EMAIL,
    FIXED_AUTHOR_NAME,
    FIXED_DATE,
    materialize,
)

ROOT = Path(__file__).resolve().parent
CASES_PATH = ROOT / "fixtures" / "retrieval" / "cases.json"
TREES = ROOT / "fixtures" / "retrieval" / "trees"
K_VALUES = (1, 3, 5, 10)
V1_PROMPT_BUDGET = 2048
COST_PREFIX_BUDGETS = (256, 512)
GIT_SIGNAL_REPO = "dev_auth"
GIT_SIGNAL_QUERY = "login session"
GIT_SIGNAL_TASK = "bugfix"
GIT_SIGNAL_LABELED = "auth/session.py"
GIT_SIGNAL_DECOY = "decoy.py"

log = logging.getLogger("lean_weave.benchmarks.retrieval")


@dataclass(frozen=True)
class Case:
    id: str
    repository: str
    split: str
    task_kind: str
    query: str
    relevant_paths: tuple[str, ...]
    symbols: tuple[dict[str, Any], ...]
    expected_ranges: tuple[dict[str, Any], ...]
    relevant_ranges: tuple[dict[str, Any], ...] = ()
    required_dependencies: tuple[dict[str, Any], ...] = ()
    judge_schema_version: int = 1
    fingerprint: str = ""


@dataclass(frozen=True)
class RankedHit:
    path: str
    qualified_name: str | None
    definition_start_line: int | None
    start_line: int
    end_line: int
    estimated_token_cost: int
    ranking_reasons: tuple[str, ...]


def _tree_root(repository: str) -> Path:
    retrieval = TREES / repository
    if retrieval.is_dir():
        return retrieval
    pinned = TREES.parent.parent / "trees" / repository
    if pinned.is_dir():
        return pinned
    raise ValueError(f"unknown_repository:{repository}")


def _tree_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if not path.is_file() or path.is_symlink():
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _canonical_label(entry: Any, *, case_id: str, root: Path) -> dict[str, Any]:
    if not isinstance(entry, Mapping):
        raise ValueError(f"invalid_label:{case_id}")
    path = entry.get("path")
    if not isinstance(path, str) or not path or path.startswith("/") or ".." in path.split("/"):
        raise ValueError(f"invalid_label_path:{case_id}")
    target = root / path
    if not target.is_file():
        raise ValueError(f"stale_label_path:{case_id}:{path}")
    try:
        line_count = len(target.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeError):
        raise ValueError(f"unreadable_label_path:{case_id}:{path}") from None
    start = entry.get("start_line")
    end = entry.get("end_line")
    if start is None and end is None:
        return {"path": path, "start_line": 1, "end_line": line_count, "whole_file": True}
    if isinstance(start, int) and end is None:
        end = start
    if start is None and isinstance(end, int):
        start = 1
    if (
        isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, int)
        or not isinstance(end, int)
        or start < 1
        or end < start
        or end > line_count
    ):
        raise ValueError(f"stale_label_range:{case_id}:{path}")
    return {"path": path, "start_line": start, "end_line": end}


def load_cases() -> list[Case]:
    raw = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("retrieval cases must be a list")
    cases: list[Case] = []
    seen_ids: set[str] = set()
    fingerprints: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("invalid_retrieval_case")
        case_id = str(item.get("id") or "")
        if not case_id or case_id in seen_ids:
            raise ValueError(f"duplicate_case:{case_id}")
        seen_ids.add(case_id)
        repository = str(item["repository"])
        root = _tree_root(repository)
        fingerprint = fingerprints.setdefault(repository, _tree_fingerprint(root))
        paths = tuple(item["relevant_paths"])
        if not paths or len(set(paths)) != len(paths):
            raise ValueError(f"case {case_id} missing or duplicate relevant_paths")
        for path in paths:
            _canonical_label({"path": path}, case_id=case_id, root=root)
        relevant_ranges = tuple(
            _canonical_label(entry, case_id=case_id, root=root)
            for entry in item.get("relevant_ranges") or ()
        )
        expected_ranges = tuple(
            _canonical_label(entry, case_id=case_id, root=root)
            for entry in item.get("expected_ranges") or ()
        )
        dependencies = tuple(
            _canonical_label(entry, case_id=case_id, root=root)
            for entry in item.get("required_dependencies") or ()
        )
        symbols = tuple(item.get("symbols") or ())
        symbol_keys: set[tuple[str, str, int]] = set()
        for symbol in symbols:
            labeled = _canonical_label(symbol, case_id=case_id, root=root)
            name = symbol.get("qualified_name")
            start = symbol.get("start_line")
            if not isinstance(name, str) or not name or not isinstance(start, int):
                raise ValueError(f"invalid_symbol_label:{case_id}")
            if labeled["start_line"] != labeled["end_line"]:
                if not (labeled["start_line"] <= start <= labeled["end_line"]):
                    raise ValueError(f"stale_symbol_label:{case_id}")
            key = (labeled["path"], name, start)
            if key in symbol_keys:
                raise ValueError(f"duplicate_symbol_label:{case_id}")
            symbol_keys.add(key)
        cases.append(
            Case(
                id=case_id,
                repository=repository,
                split=str(item["split"]),
                task_kind=str(item["task_kind"]),
                query=str(item["query"]),
                relevant_paths=paths,
                symbols=symbols,
                expected_ranges=expected_ranges,
                relevant_ranges=relevant_ranges,
                required_dependencies=dependencies,
                fingerprint=fingerprint,
            )
        )
    return sorted(cases, key=lambda item: item.id)


def _copy_tree(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(
        src,
        dest,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
    )


def materialize_repository(name: str, scratch: Path) -> Path:
    dest = scratch / "repos" / name
    if name in {"bug_fix", "multi_file_feature"}:
        fixture = materialize(name, scratch / "pinned" / name)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(fixture.root, dest)
        return dest
    source = TREES / name
    if not source.is_dir():
        raise FileNotFoundError(name)
    _copy_tree(source, dest)
    return dest


def precision_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    chosen = []
    seen: set[str] = set()
    for path in ranked:
        if path in seen:
            continue
        seen.add(path)
        chosen.append(path)
        if len(chosen) >= k:
            break
    hits = sum(1 for path in chosen if path in relevant)
    return hits / k if k else 0.0


def recall_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    chosen = []
    seen: set[str] = set()
    for path in ranked:
        if path in seen:
            continue
        seen.add(path)
        chosen.append(path)
        if len(chosen) >= k:
            break
    hits = sum(1 for path in chosen if path in relevant)
    return hits / len(relevant)


def _v1_rank(root: Path, query: str) -> tuple[list[str], list[tuple[str, str, int]]]:
    repository = Repository(root, ContextConfig())
    index = build_python_index(repository)
    ranked = rank(
        repository,
        index,
        ContextQuery(task=query, include_tests=True, allow_git=False),
    )
    files = [item.path for item in ranked.files]
    symbols = [
        (item.path, item.qualified_name, item.start_line) for item in ranked.symbols
    ]
    return files, symbols


def _v1_prompt_excerpts(root: Path, query: str) -> dict[str, Any]:
    """Actual V1 prompt excerpts — separate from ranking metrics."""

    config = ContextConfig(stage_prompt_tokens_max=V1_PROMPT_BUDGET)
    repository = Repository(root, config)
    index = build_python_index(repository)
    result = ContextBuilder(repository, index=index).build(
        ContextQuery(
            task=query,
            level=ExpansionLevel.RANGES,
            include_tests=True,
            allow_git=False,
        )
    )
    pack = result.pack
    log.debug(
        "[FIX] v1 prompt observation budget=%s excerpts=%s estimated=%s",
        V1_PROMPT_BUDGET,
        len(pack.excerpts),
        pack.estimated_tokens,
    )
    return {
        "observation": "v1_prompt_excerpts",
        "budget_tokens": V1_PROMPT_BUDGET,
        "estimated_tokens": pack.estimated_tokens,
        "required_tokens": pack.required_tokens,
        "excerpts": [
            {
                "path": item.path,
                "start_line": item.start_line,
                "end_line": item.end_line,
                "kind": item.kind,
                "symbol": item.symbol,
                "digest": item.digest,
                "text": item.text,
            }
            for item in pack.excerpts
        ],
    }


def _hit_summary(hit) -> RankedHit:
    return RankedHit(
        path=hit.path,
        qualified_name=hit.qualified_name,
        definition_start_line=hit.definition_start_line,
        start_line=hit.start_line,
        end_line=hit.end_line,
        estimated_token_cost=hit.estimated_token_cost,
        ranking_reasons=tuple(item.signal for item in hit.ranking_reasons),
    )


def _v2_rank(
    root: Path,
    case: Case,
    *,
    cache: Path,
    warm: bool,
    read_diff=None,
    build_index: bool | None = None,
) -> tuple[list[RankedHit], list[RankedHit], list[str]]:
    repository = Repository(root, ContextConfig(), strict_source=True)
    index = CodeIndex(root, cache_base=cache, context_config=ContextConfig())
    should_build = warm if build_index is None else build_index
    if should_build:
        index.build()
    service = RepositoryMap(index)

    def load_graph(scan):
        return service.graph_for_retrieval_scan(scan)

    retriever = ContextRetriever(repository, load_graph=load_graph, read_diff=read_diff)
    reasons: list[str] = []
    file_result = retriever.retrieve(
        RetrievalRequest(
            query=case.query,
            task_kind=case.task_kind,  # type: ignore[arg-type]
            kind="file",
            top_k=10,
        )
    )
    reasons.extend(file_result.degraded_reasons)
    symbol_result = retriever.retrieve(
        RetrievalRequest(
            query=case.query,
            task_kind=case.task_kind,  # type: ignore[arg-type]
            kind="symbol",
            top_k=10,
        )
    )
    reasons.extend(symbol_result.degraded_reasons)
    files = [_hit_summary(hit) for hit in file_result.hits]
    symbols = [_hit_summary(hit) for hit in symbol_result.hits]
    return files, symbols, sorted(set(reasons))


def _symbol_matches(
    ranked: Sequence[tuple[str, str, int]], gold: Sequence[Mapping[str, Any]], k: int
) -> tuple[float, float]:
    if not gold:
        return 0.0, 0.0
    truth = {
        (item["path"], item["qualified_name"], int(item["start_line"])) for item in gold
    }
    chosen = []
    seen: set[tuple[str, str, int]] = set()
    for item in ranked:
        if item in seen:
            continue
        seen.add(item)
        chosen.append(item)
        if len(chosen) >= k:
            break
    hits = sum(1 for item in chosen if item in truth)
    return (hits / k if k else 0.0), (hits / len(truth))


def _covers_interval(hit: RankedHit, interval: Mapping[str, Any]) -> bool:
    return (
        hit.path == interval["path"]
        and hit.start_line <= int(interval["start_line"])
        and hit.end_line >= int(interval["end_line"])
    )


def cost_metrics(
    hits: Sequence[RankedHit],
    expected_ranges: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    cumulative = {
        f"cost@{k}": int(sum(hit.estimated_token_cost for hit in hits[:k]))
        for k in K_VALUES
    }
    gold_covered = False
    if expected_ranges:
        gold_covered = any(
            _covers_interval(hit, interval)
            for hit in hits
            for interval in expected_ranges
        )

    prefixes: dict[str, Any] = {}
    for budget in COST_PREFIX_BUDGETS:
        total = 0
        covered = 0
        seen_intervals: set[tuple[str, int, int]] = set()
        included = 0
        for hit in hits:
            next_total = total + hit.estimated_token_cost
            if next_total > budget:
                break
            total = next_total
            included += 1
            for interval in expected_ranges:
                key = (
                    str(interval["path"]),
                    int(interval["start_line"]),
                    int(interval["end_line"]),
                )
                if key in seen_intervals:
                    continue
                if _covers_interval(hit, interval):
                    seen_intervals.add(key)
                    covered += 1
        prefixes[f"prefix_{budget}"] = {
            "budget": budget,
            "prefix_hits": included,
            "prefix_cost": total,
            "gold_intervals_covered": covered,
            "gold_interval_count": len(expected_ranges),
        }

    return {
        "cumulative": cumulative,
        "gold_range_covered": gold_covered,
        **prefixes,
    }


def evaluate_case(case: Case, scratch: Path) -> dict[str, Any]:
    root = materialize_repository(case.repository, scratch)
    relevant = set(case.relevant_paths)
    for path in relevant:
        if not (root / path).exists():
            raise FileNotFoundError(f"{case.id}:{path}")

    v1_files, v1_symbols = _v1_rank(root, case.query)
    v1_prompt = _v1_prompt_excerpts(root, case.query)
    cache = scratch / "cache" / case.id
    cache.mkdir(parents=True, mode=0o700, exist_ok=True)
    cold_files, cold_symbols, cold_reasons = _v2_rank(
        root, case, cache=cache, warm=False, read_diff=None
    )
    warm_files, warm_symbols, warm_reasons = _v2_rank(
        root, case, cache=cache, warm=True, read_diff=None
    )

    def file_paths(hits: Sequence[RankedHit]) -> list[str]:
        return [hit.path for hit in hits]

    def symbol_keys(hits: Sequence[RankedHit]) -> list[tuple[str, str, int]]:
        return [
            (hit.path, hit.qualified_name or "", hit.definition_start_line or 0)
            for hit in hits
        ]

    def metrics(
        files: Sequence[str] | Sequence[RankedHit],
        symbols: Sequence[tuple[str, str, int]] | Sequence[RankedHit],
        engine: str,
        *,
        file_hits: Sequence[RankedHit] | None = None,
        symbol_hits: Sequence[RankedHit] | None = None,
    ) -> dict[str, Any]:
        if file_hits is not None:
            paths = file_paths(file_hits)
            sym_keys = symbol_keys(symbol_hits or ())
        else:
            paths = list(files)  # type: ignore[arg-type]
            sym_keys = list(symbols)  # type: ignore[arg-type]
        file_metrics = {
            f"P@{k}": round(precision_at_k(paths, relevant, k), 4) for k in K_VALUES
        }
        file_metrics.update(
            {f"R@{k}": round(recall_at_k(paths, relevant, k), 4) for k in K_VALUES}
        )
        symbol_metrics: dict[str, float] = {}
        if case.symbols:
            for k in K_VALUES:
                p, r = _symbol_matches(sym_keys, case.symbols, k)
                symbol_metrics[f"sym_P@{k}"] = round(p, 4)
                symbol_metrics[f"sym_R@{k}"] = round(r, 4)
        payload: dict[str, Any] = {
            "engine": engine,
            "files": paths,
            "file_metrics": file_metrics,
            "symbol_metrics": symbol_metrics,
            "first_wrong": next((path for path in paths if path not in relevant), None),
            "misses": sorted(relevant - set(paths[:10])),
        }
        if file_hits is not None:
            payload["cost"] = cost_metrics(file_hits, case.expected_ranges)
            if symbol_hits is not None and case.expected_ranges:
                payload["symbol_cost"] = cost_metrics(symbol_hits, case.expected_ranges)
        return payload

    return {
        "id": case.id,
        "repository": case.repository,
        "split": case.split,
        "task_kind": case.task_kind,
        "v1": metrics(v1_files, v1_symbols, "v1"),
        "v1_prompt": v1_prompt,
        "v2_cold": {
            **metrics(
                (),
                (),
                "v2_cold",
                file_hits=cold_files,
                symbol_hits=cold_symbols,
            ),
            "degraded_reasons": cold_reasons,
        },
        "v2_warm": {
            **metrics(
                (),
                (),
                "v2_warm",
                file_hits=warm_files,
                symbol_hits=warm_symbols,
            ),
            "degraded_reasons": warm_reasons,
        },
    }


@dataclass(frozen=True)
class RetrievalVariant:
    name: str
    strategy: str
    map_tokens: int | None = None
    source_tokens: int | None = None
    use_graph: bool = True
    cache: str = "disabled"

    def conditions(self, base: ContextConfig) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "map_tokens": (
                base.packing.map_tokens if self.map_tokens is None else self.map_tokens
            ),
            "source_tokens": (
                base.packing.source_tokens
                if self.source_tokens is None
                else self.source_tokens
            ),
            "graph": self.use_graph,
            "cache": self.cache,
        }


RETRIEVAL_VARIANTS: tuple[RetrievalVariant, ...] = (
    RetrievalVariant("v1_default", "v1"),
    RetrievalVariant("v2_default", "v2"),
    RetrievalVariant("v2_map_off", "v2", map_tokens=0),
    RetrievalVariant("v2_no_graph", "v2", map_tokens=0, use_graph=False),
    RetrievalVariant("v2_tight", "v2", source_tokens=512),
    RetrievalVariant("v2_cache_cold", "v2", cache="cold"),
    RetrievalVariant("v2_cache_warm", "v2", cache="warm"),
    RetrievalVariant("v2_cache_disabled", "v2", cache="disabled"),
)
VARIANT_NAMES = tuple(item.name for item in RETRIEVAL_VARIANTS)


def _variant(name: str | RetrievalVariant) -> RetrievalVariant:
    if isinstance(name, RetrievalVariant):
        return name
    for item in RETRIEVAL_VARIANTS:
        if item.name == name:
            return item
    raise ValueError(f"unknown_retrieval_variant:{name}")


def _union_spans(spans: Sequence[Mapping[str, Any]]) -> dict[str, list[tuple[int, int]]]:
    grouped: dict[str, list[tuple[int, int]]] = {}
    for span in spans:
        path = str(span["path"])
        start = int(span.get("start_line") or 1)
        end = int(span.get("end_line") or start)
        grouped.setdefault(path, []).append((start, end))
    merged: dict[str, list[tuple[int, int]]] = {}
    for path, values in grouped.items():
        result: list[tuple[int, int]] = []
        for start, end in sorted(values):
            if result and start <= result[-1][1] + 1:
                result[-1] = (result[-1][0], max(result[-1][1], end))
            else:
                result.append((start, end))
        merged[path] = result
    return merged


def _intersection(
    left: dict[str, list[tuple[int, int]]],
    right: dict[str, list[tuple[int, int]]],
) -> dict[str, list[tuple[int, int]]]:
    result: dict[str, list[tuple[int, int]]] = {}
    for path in sorted(set(left) & set(right)):
        spans: list[tuple[int, int]] = []
        for a_start, a_end in left[path]:
            for b_start, b_end in right[path]:
                start = max(a_start, b_start)
                end = min(a_end, b_end)
                if start <= end:
                    spans.append((start, end))
        if spans:
            result[path] = _union_spans(
                [{"path": path, "start_line": s, "end_line": e} for s, e in spans]
            )[path]
    return result


def _span_tokens(root: Path, spans: dict[str, list[tuple[int, int]]]) -> int:
    total = 0
    for path, intervals in spans.items():
        try:
            lines = (root / path).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for start, end in intervals:
            text = "\n".join(lines[max(0, start - 1) : end])
            total += estimate_tokens(text)
    return total


def _covered(
    spans: dict[str, list[tuple[int, int]]],
    required: Mapping[str, Any],
) -> bool:
    path = str(required["path"])
    start = int(required["start_line"])
    end = int(required["end_line"])
    return any(a <= start and b >= end for a, b in spans.get(path, ()))


def _precision_recall(ranked: Sequence[str], relevant: set[str], k: int) -> tuple[float, float]:
    unique: list[str] = []
    seen: set[str] = set()
    for item in ranked:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    top = unique[:k]
    hits = sum(1 for item in top if item in relevant)
    precision = hits / k
    recall = hits / len(relevant) if relevant else 0.0
    return round(precision, 4), round(recall, 4)


def _symbol_precision_recall(
    ranked: Sequence[tuple[str, str, int]],
    truth: set[tuple[str, str, int]],
    k: int,
) -> tuple[float, float]:
    unique: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str, int]] = set()
    for item in ranked:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    top = unique[:k]
    hits = sum(1 for item in top if item in truth)
    precision = hits / k
    recall = hits / len(truth) if truth else 0.0
    return round(precision, 4), round(recall, 4)


def _v1_spans(root: Path, query: str, config: ContextConfig) -> tuple[list[str], list[tuple[str, str, int]], list[dict[str, Any]], int]:
    repository = Repository(root, config, strict_source=True)
    index = build_python_index(repository)
    ranked = rank(
        repository,
        index,
        ContextQuery(task=query, include_tests=True, allow_git=False),
    )
    files = [item.path for item in ranked.files]
    symbols = [
        (item.path, item.qualified_name, item.start_line) for item in ranked.symbols
    ]
    built = ContextBuilder(repository, config=config, index=index).build(
        ContextQuery(
            task=query,
            level=ExpansionLevel.RANGES,
            include_tests=True,
            allow_git=False,
        )
    )
    spans = [
        {"path": item.path, "start_line": item.start_line, "end_line": item.end_line}
        for item in built.pack.excerpts
    ]
    return files, symbols, spans, built.pack.estimated_tokens


def _overlap_lines(spans: Sequence[Mapping[str, Any]]) -> int:
    """Line count emitted more than once by pre-union overlapping spans."""

    grouped: dict[str, list[tuple[int, int]]] = {}
    for span in spans:
        path = str(span["path"])
        start = int(span.get("start_line") or 1)
        end = int(span.get("end_line") or start)
        if end >= start:
            grouped.setdefault(path, []).append((start, end))
    total = 0
    for values in grouped.values():
        covered: set[int] = set()
        emitted = 0
        for start, end in values:
            emitted += end - start + 1
            covered.update(range(start, end + 1))
        total += max(0, emitted - len(covered))
    return total


def _missing_symbols(
    selected_union: dict[str, list[tuple[int, int]]],
    symbols: Sequence[Mapping[str, Any]],
) -> list[str]:
    missing: list[str] = []
    for item in symbols:
        path = str(item.get("path"))
        start = int(item.get("start_line") or 1)
        end = int(item.get("end_line") or start)
        if not _covered(selected_union, {"path": path, "start_line": start, "end_line": end}):
            missing.append(f"{path}:{start}:{item.get('qualified_name')}")
    return sorted(missing)


def _v2_spans(
    root: Path,
    case: Case,
    variant: RetrievalVariant,
    config: ContextConfig,
    cache_base: Path,
) -> tuple[list[str], list[tuple[str, str, int]], list[dict[str, Any]], dict[str, Any]]:
    cache = None
    cache_counts = {"hits": 0, "misses": 0, "available": False}
    if variant.cache in {"cold", "warm"}:
        cache = SqliteContextCache(
            root,
            config=config.cache,
            context_config=config,
            cache_base=cache_base,
        )

        def observe_cache(kind, result, reason=None) -> None:
            if result == "hit":
                cache_counts["hits"] += 1
            elif result in {"miss", "invalidated", "bypass"}:
                cache_counts["misses"] += 1

        cache.observer = observe_cache
        cache_counts["available"] = True
    observations: dict[str, Any] = {
        "ranked_visible": False,
        "cache": cache_counts,
        "graph_loaded": False,
        "index_built": False,
    }
    repository = Repository(root, config, strict_source=True)
    index = CodeIndex(root, cache_base=cache_base, context_config=config)
    effective_map_tokens = config.packing.map_tokens
    need_index = variant.use_graph or effective_map_tokens > 0
    if need_index:
        try:
            index.build()
            observations["index_built"] = True
        except Exception:  # noqa: BLE001 - evaluation degrades rather than fails
            observations["index_error"] = True
    service = RepositoryMap(index)

    def load_graph(scan):
        observations["graph_loaded"] = True
        return service.graph_for_retrieval_scan(scan)

    retriever = ContextRetriever(
        repository,
        load_graph=load_graph if variant.use_graph else None,
        cache=cache,
    )
    observed: dict[str, Any] = {"result": None}

    def on_ranked(result) -> None:
        observed["result"] = result if hasattr(result, "hits") else getattr(result, "result", None)

    packer = ContextPacker(
        repository,
        retriever=retriever,
        model="deepseek-flash",
        thinking=False,
        cache=cache,
        rank_observer=on_ranked,
    )
    request = PackRequest(
        task_id=f"eval-{case.id}",
        query=case.query,
        task_specification=case.query,
        system_instructions="Evaluate retrieval against pinned fixture labels.",
        level="l1",
        envelope=PackEnvelope(model="deepseek-flash", thinking="disabled"),
        allow_empty_map=True,
    )
    if variant.cache == "warm":
        prime = packer.build(request)
        observations["prime_status"] = prime.status
    started = time.monotonic()
    result = packer.build(request)
    observations["pack_ms"] = int((time.monotonic() - started) * 1000)
    spans = []
    pack = result.pack
    if pack is not None:
        spans = [
            {
                "path": fragment.path,
                "start_line": fragment.start_line,
                "end_line": fragment.end_line,
            }
            for fragment in pack.fragments
        ]
        observations["pack_metrics"] = pack.metrics.model_dump(mode="json")
        observations["map_rows"] = len(pack.map_rows)
        observations["section_tokens"] = dict(pack.metrics.section_tokens)
        observations["selected_files"] = len({fragment.path for fragment in pack.fragments})
        observations["selected_symbols"] = sum(
            1 for fragment in pack.fragments if fragment.kind == "definition"
        )
    observations["pack_status"] = result.status
    observations["reason_codes"] = list(result.reason_codes or ())
    ranked = observed["result"]
    hits = tuple(getattr(ranked, "hits", ()) or ())
    observations["ranked_visible"] = bool(hits)
    observations["files"] = [hit.path for hit in hits]
    files = [
        hit.path
        for hit in hits
        if getattr(hit, "kind", "file") in {"file", "symbol"}
    ]
    seen_files: set[str] = set()
    files = [path for path in files if not (path in seen_files or seen_files.add(path))]
    symbols = [
        (hit.path, hit.qualified_name or "", hit.definition_start_line or 0)
        for hit in hits
        if getattr(hit, "kind", None) == "symbol"
    ]
    return files, symbols, spans, observations


def evaluate_retrieval(
    case: Case | str,
    variant: str | RetrievalVariant = "v2_default",
    scratch: Path | None = None,
) -> dict[str, Any]:
    """Score ranked relevance and actual packed source separately.

    This is a deterministic local estimate. It never calls an LLM and never
    claims provider token savings.
    """

    if isinstance(case, str):
        selected = next((item for item in load_cases() if item.id == case), None)
        if selected is None:
            raise ValueError(f"unknown_retrieval_case:{case}")
        case = selected
    spec = _variant(variant)
    own_scratch = scratch is None
    scratch_dir = Path(tempfile.mkdtemp(prefix="lw-retrieval-eval-")) if own_scratch else Path(scratch)
    try:
        root = materialize_repository(case.repository, scratch_dir / case.id / spec.name)
        base_config = ContextConfig()
        config = base_config
        if spec.map_tokens is not None or spec.source_tokens is not None:
            config = base_config.model_copy(
                update={
                    "packing": base_config.packing.model_copy(
                        update={
                            "map_tokens": (
                                base_config.packing.map_tokens
                                if spec.map_tokens is None
                                else spec.map_tokens
                            ),
                            "source_tokens": (
                                base_config.packing.source_tokens
                                if spec.source_tokens is None
                                else spec.source_tokens
                            ),
                        }
                    )
                }
            )
        cache_base = scratch_dir / "cache" / case.id / spec.name
        cache_base.mkdir(parents=True, mode=0o700, exist_ok=True)
        started = time.monotonic()
        if spec.strategy == "v1":
            files, symbols, spans, estimated = _v1_spans(root, case.query, config)
            observations = {
                "pack_status": "ready" if spans else "refused",
                "reason_codes": [] if spans else ["no_source_selected"],
                "selected_files": len({item["path"] for item in spans}),
                "selected_symbols": None,
                "estimated_tokens": estimated,
            }
        else:
            files, symbols, spans, observations = _v2_spans(
                root, case, spec, config, cache_base
            )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        relevant_paths = set(case.relevant_paths)
        file_scores = {}
        for k in K_VALUES:
            precision, recall = _precision_recall(files, relevant_paths, k)
            file_scores[f"P@{k}"] = precision
            file_scores[f"R@{k}"] = recall
        symbol_scores: dict[str, Any] = {
            "applicable": bool(case.symbols),
            "denominator": len(case.symbols),
        }
        if case.symbols:
            truth = {
                (item["path"], item["qualified_name"], int(item["start_line"]))
                for item in case.symbols
            }
            for k in K_VALUES:
                precision, recall = _symbol_precision_recall(symbols, truth, k)
                symbol_scores[f"P@{k}"] = precision
                symbol_scores[f"R@{k}"] = recall
        labeled_spans = tuple(case.relevant_ranges) or tuple(case.expected_ranges)
        relevant_union = _union_spans(labeled_spans)
        selected_union = _union_spans(spans)
        covered = _intersection(relevant_union, selected_union)
        relevant_tokens = _span_tokens(root, covered)
        selected_tokens = _span_tokens(root, selected_union)
        missing_dependencies = sum(
            1 for item in case.required_dependencies if not _covered(selected_union, item)
        )
        missing_symbols = _missing_symbols(selected_union, case.symbols)
        pack_input_tokens = observations.get("pack_metrics", {}).get("input_tokens")
        deduplicated = observations.get("pack_metrics", {}).get("deduplicated")
        return {
            "schema_version": 1,
            "case_id": case.id,
            "repository": case.repository,
            "split": case.split,
            "task_kind": case.task_kind,
            "query": case.query,
            "variant": spec.name,
            "provenance": "local_estimate",
            "judge_schema_version": case.judge_schema_version,
            "fixture_fingerprint": case.fingerprint,
            "conditions": spec.conditions(config),
            "estimator": "conservative_estimate",
            "ranking": {
                "files": file_scores,
                "symbols": symbol_scores,
                "denominators": {
                    "relevant_files": len(relevant_paths),
                    "relevant_symbols": len(case.symbols),
                },
            },
            "pack": {
                "status": observations.get("pack_status"),
                "reason_codes": observations.get("reason_codes", []),
                "selected_files": observations.get("selected_files"),
                "selected_symbols": observations.get("selected_symbols"),
                "selected_source_tokens": selected_tokens,
                "unique_source_tokens": selected_tokens,
                "pack_estimated_input_tokens": pack_input_tokens,
                "deduplicated": deduplicated,
                "emitted_duplicate_lines": _overlap_lines(spans),
                "relevant_source_tokens": relevant_tokens,
                "relevant_token_ratio": (
                    round(relevant_tokens / selected_tokens, 4)
                    if selected_tokens
                    else None
                ),
                "required_dependency_count": len(case.required_dependencies),
                "missing_dependency_count": missing_dependencies,
                "required_symbol_count": len(case.symbols),
                "missing_symbol_count": len(missing_symbols),
                "missing_symbols": missing_symbols,
                "map_rows": observations.get("map_rows"),
                "available": bool(selected_tokens),
            },
            "cache": {
                "state": spec.cache,
                "enabled": spec.cache != "disabled",
                "available": bool(observations.get("cache", {}).get("available")),
                "hits": observations.get("cache", {}).get("hits", 0),
                "misses": observations.get("cache", {}).get("misses", 0),
            },
            "timing_ms": {
                "total": elapsed_ms,
                "pack": observations.get("pack_ms"),
            },
            "degraded_reasons": [],
            "observations": {
                key: value
                for key, value in observations.items()
                if key not in {"pack_metrics"}
            },
        }
    finally:
        if own_scratch:
            shutil.rmtree(scratch_dir, ignore_errors=True)


def evaluate_variants(
    case: Case | str,
    variants: Sequence[str] | None = None,
    scratch: Path | None = None,
) -> dict[str, Any]:
    selected = list(variants or VARIANT_NAMES)
    rows = [evaluate_retrieval(case, name, scratch) for name in selected]
    return {
        "schema_version": 1,
        "case_id": rows[0]["case_id"] if rows else (case if isinstance(case, str) else case.id),
        "variants": rows,
    }


def macro_average(rows: Sequence[Mapping[str, Any]], engine: str, split: str) -> dict[str, float]:
    selected = [row[engine]["file_metrics"] for row in rows if row["split"] == split]
    if not selected:
        return {}
    keys = sorted(selected[0])
    return {
        key: round(sum(item[key] for item in selected) / len(selected), 4) for key in keys
    }


def _git_env(home: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": FIXED_AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": FIXED_AUTHOR_EMAIL,
        "GIT_COMMITTER_NAME": FIXED_AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": FIXED_AUTHOR_EMAIL,
        "GIT_AUTHOR_DATE": FIXED_DATE,
        "GIT_COMMITTER_DATE": FIXED_DATE,
        "LANG": "C",
        "LC_ALL": "C",
    }


def _run_git(root: Path, *args: str) -> None:
    home = root / ".fixture-home"
    home.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_git_env(home),
    )


def production_read_diff(root: Path, admitted_paths):
    return read_retrieval_diff(root, admitted_paths, run_capture=run_bounded)


def run_git_signal_comparison(scratch: Path) -> dict[str, Any]:
    """Isolate Git boost on a dirty development checkout (not part of V1 baseline)."""

    log.debug("[FIX] git signal comparison start repo=%s", GIT_SIGNAL_REPO)
    root = materialize_repository(GIT_SIGNAL_REPO, scratch / "git-signal")
    _run_git(root, "init", "-q", "-b", "main")
    _run_git(root, "config", "user.email", FIXED_AUTHOR_EMAIL)
    _run_git(root, "config", "user.name", FIXED_AUTHOR_NAME)
    _run_git(root, "config", "core.autocrlf", "false")
    _run_git(root, "config", "core.filemode", "true")
    tracked = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts and ".fixture-home" not in path.parts
    )
    _run_git(root, "add", "--", *tracked)
    _run_git(root, "commit", "-qm", COMMIT_MESSAGE)

    labeled = root / GIT_SIGNAL_LABELED
    decoy = root / GIT_SIGNAL_DECOY
    labeled.write_text(
        labeled.read_text(encoding="utf-8") + "# checkout noise\n",
        encoding="utf-8",
    )
    decoy.write_text(
        decoy.read_text(encoding="utf-8") + "# checkout noise\n",
        encoding="utf-8",
    )

    case = Case(
        id="git_signal_dev_auth",
        repository=GIT_SIGNAL_REPO,
        split="development",
        task_kind=GIT_SIGNAL_TASK,
        query=GIT_SIGNAL_QUERY,
        relevant_paths=(GIT_SIGNAL_LABELED,),
        symbols=(),
        expected_ranges=(),
    )
    cache = scratch / "git-signal-cache"
    cache.mkdir(parents=True, mode=0o700, exist_ok=True)
    # Build once after dirty edits; reuse that checkout/cache for both queries.
    CodeIndex(root, cache_base=cache, context_config=ContextConfig()).build()
    log.debug("[FIX] git signal index built once cache=%s", cache.name)

    without_files, _, without_reasons = _v2_rank(
        root,
        case,
        cache=cache,
        warm=True,
        read_diff=None,
        build_index=False,
    )
    with_files, _, with_reasons = _v2_rank(
        root,
        case,
        cache=cache,
        warm=True,
        read_diff=production_read_diff,
        build_index=False,
    )

    def summarize(hits: Sequence[RankedHit]) -> dict[str, Any]:
        labeled_hit = next((hit for hit in hits if hit.path == GIT_SIGNAL_LABELED), None)
        decoy_hit = next((hit for hit in hits if hit.path == GIT_SIGNAL_DECOY), None)
        return {
            "files": [hit.path for hit in hits],
            "labeled_rank": next(
                (index for index, hit in enumerate(hits) if hit.path == GIT_SIGNAL_LABELED),
                None,
            ),
            "labeled_has_git_diff": bool(
                labeled_hit and "git_diff" in labeled_hit.ranking_reasons
            ),
            "decoy_present": decoy_hit is not None,
            "decoy_has_git_diff": bool(
                decoy_hit and "git_diff" in decoy_hit.ranking_reasons
            ),
            "labeled_score_reasons": list(labeled_hit.ranking_reasons)
            if labeled_hit
            else [],
        }

    without = summarize(without_files)
    with_git = summarize(with_files)
    log.debug(
        "[FIX] git signal labeled_boost=%s decoy_git=%s",
        with_git["labeled_has_git_diff"],
        with_git["decoy_has_git_diff"],
    )
    return {
        "observation": "git_signal_dirty_checkout",
        "repository": GIT_SIGNAL_REPO,
        "query": GIT_SIGNAL_QUERY,
        "labeled_path": GIT_SIGNAL_LABELED,
        "decoy_path": GIT_SIGNAL_DECOY,
        "read_diff_none": {**without, "degraded_reasons": without_reasons},
        "production_diff": {**with_git, "degraded_reasons": with_reasons},
        "rank_changed": without["files"] != with_git["files"]
        or without["labeled_rank"] != with_git["labeled_rank"],
    }


def run_comparison(*, as_json: bool = False) -> dict[str, Any]:
    cases = load_cases()
    with tempfile.TemporaryDirectory(prefix="lw-retrieval-") as tmp:
        scratch = Path(tmp)
        rows = [evaluate_case(case, scratch) for case in cases]
        git_signal = run_git_signal_comparison(scratch)
    report = {
        "schema": "lw-retrieval-comparison-v1",
        "k_values": list(K_VALUES),
        "cost_prefix_budgets": list(COST_PREFIX_BUDGETS),
        "v1_prompt_budget_tokens": V1_PROMPT_BUDGET,
        "cases": rows,
        "macro": {
            "development": {
                engine: macro_average(rows, engine, "development")
                for engine in ("v1", "v2_cold", "v2_warm")
            },
            "holdout": {
                engine: macro_average(rows, engine, "holdout")
                for engine in ("v1", "v2_cold", "v2_warm")
            },
        },
        "git_signal": git_signal,
    }
    if as_json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        for split, engines in report["macro"].items():
            print(f"## {split}")
            for engine, metrics in engines.items():
                print(engine, metrics)
        git = report["git_signal"]
        print(
            "## git_signal",
            {
                "labeled_has_git_diff": git["production_diff"]["labeled_has_git_diff"],
                "decoy_has_git_diff": git["production_diff"]["decoy_has_git_diff"],
                "rank_changed": git["rank_changed"],
            },
        )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    run_comparison(as_json=args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
