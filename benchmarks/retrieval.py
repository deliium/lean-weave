"""Offline V1 vs V2 retrieval quality comparison (developer tooling)."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from lean_weave.code_index import CodeIndex
from lean_weave.config.models import ContextConfig
from lean_weave.context.builder import ContextBuilder
from lean_weave.context.git_metadata import read_retrieval_diff
from lean_weave.context.models import ContextQuery, ExpansionLevel
from lean_weave.context.python_index import build_python_index
from lean_weave.context.ranking import rank
from lean_weave.context.repository import Repository
from lean_weave.context.retrieval_models import RetrievalRequest
from lean_weave.context.retriever import ContextRetriever
from lean_weave.repository_map import RepositoryMap
from lean_weave.verify.process import run_bounded

from benchmarks.fixtures import (
    COMMIT_MESSAGE,
    FIXED_AUTHOR_EMAIL,
    FIXED_AUTHOR_NAME,
    FIXED_DATE,
    materialize,
)

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "benchmarks" / "fixtures" / "retrieval" / "cases.json"
TREES = ROOT / "benchmarks" / "fixtures" / "retrieval" / "trees"
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


@dataclass(frozen=True)
class RankedHit:
    path: str
    qualified_name: str | None
    definition_start_line: int | None
    start_line: int
    end_line: int
    estimated_token_cost: int
    ranking_reasons: tuple[str, ...]


def load_cases() -> list[Case]:
    raw = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases: list[Case] = []
    for item in raw:
        paths = tuple(item["relevant_paths"])
        if not paths:
            raise ValueError(f"case {item.get('id')} missing relevant_paths")
        cases.append(
            Case(
                id=str(item["id"]),
                repository=str(item["repository"]),
                split=str(item["split"]),
                task_kind=str(item["task_kind"]),
                query=str(item["query"]),
                relevant_paths=paths,
                symbols=tuple(item.get("symbols") or ()),
                expected_ranges=tuple(item.get("expected_ranges") or ()),
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
