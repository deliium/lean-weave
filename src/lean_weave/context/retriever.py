"""Opt-in offline ContextRetriever for task-aware hybrid ranking."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, Mapping

from lean_weave.context.models import iter_lines
from lean_weave.context.policy import contains_secret, is_python
from lean_weave.context.python_index import extract_python_facts, module_name_for_path
from lean_weave.context.repository import Repository, SourceScan
from lean_weave.context.retrieval_models import (
    DEGRADED_REASON_ORDER,
    DegradedReason,
    DependencyEvidence,
    RetrievalHit,
    RetrievalRequest,
    RetrievalResult,
    SignalName,
)
from lean_weave.context.retrieval_query import PreparedQuery, prepare_query
from lean_weave.context.retrieval_ranking import (
    SIGNAL_ORDER,
    RankCandidate,
    bound_matching_tests,
    dedupe_and_select,
    direct_weighted_points,
    effective_weights,
    exact_symbol_score,
    estimate_window_cost,
    is_direct_candidate,
    lexical_score,
    live_symbol_id,
    map_importance_score,
    name_score_for_file,
    name_score_for_symbol,
    ranking_reasons,
    relevance_score,
    select_definition_window,
    select_file_window,
    sort_key,
    weighted_points,
)
from lean_weave.context.search import LiteralLineMatch, search_snapshot_literals
from lean_weave.context_cache.facts import FactCache
from lean_weave.context_cache.models import ContextCache
from lean_weave.context_cache.retrieval import (
    RetrievalCache,
    RetrievalCandidateCache,
)
from lean_weave.context_cache.service import CachePolicy
from lean_weave.errors import CodeIndexError, ContextScanError, InvalidContextRequest
from lean_weave.logging_setup import get_logger

if TYPE_CHECKING:
    from lean_weave.repository_map.models import DependencyGraph, MapSymbol

log = get_logger("context.retriever")

GraphLoader = Callable[[SourceScan], "DependencyGraph"]
DiffReader = Callable[[Path, Iterable[str]], frozenset[str] | None]
RankingCallback = Callable[
    [
        "RetrievalResult",
        SourceScan,
        "DependencyGraph | None",
        tuple["RankCandidate", ...],
    ],
    object,
]


def _map_index_error(exc: CodeIndexError) -> DegradedReason:
    if exc.code == "missing":
        return "index_missing"
    if exc.code == "stale":
        return "index_stale"
    if exc.code == "incomplete":
        return "index_incomplete"
    return "index_unavailable"


def _ordered_degraded(codes: Iterable[DegradedReason]) -> tuple[DegradedReason, ...]:
    present = set(codes)
    return tuple(code for code in DEGRADED_REASON_ORDER if code in present)


@dataclass
class _Definition:
    symbol_id: str
    path: str
    qualified_name: str
    simple_name: str
    kind: str
    start_line: int
    end_line: int
    digest: str
    source_kind: str
    occurrence: int


@dataclass
class _PathState:
    snapshot: FileSnapshot
    lines: list[str]
    module_name: str
    definitions: list[_Definition] = field(default_factory=list)
    lexical: FileLiteralEvidence | None = None


class ContextRetriever:
    """Deterministic task-aware retrieval over one admitted source snapshot."""

    def __init__(
        self,
        repository: Repository,
        *,
        load_graph: GraphLoader | None = None,
        read_diff: DiffReader | None = None,
        cache: ContextCache | None = None,
        authorized_fact_peer=None,
    ) -> None:
        self.repository = repository
        self.load_graph = load_graph
        self.read_diff = read_diff
        self.authorized_fact_peer = authorized_fact_peer
        self._cache = cache if cache is not None and cache.enabled else None
        self._retrieval_cache = None
        self._candidate_cache = None
        self._fact_cache = None
        if self._cache is not None:
            policy = CachePolicy.from_context(
                repository.config,
                state_dir=repository.state_dir,
                protected_paths=repository.protected_paths,
            )
            self._retrieval_cache = RetrievalCache(self._cache, policy=policy)
            self._candidate_cache = RetrievalCandidateCache(
                self._cache, policy=policy
            )
            self._fact_cache = FactCache(self._cache, policy=policy)

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        return self._execute(request)[0]

    def rank_checked(
        self,
        request: RetrievalRequest,
        *,
        allowed_paths: frozenset[str] | None = None,
        on_ranked: RankingCallback | None = None,
    ) -> tuple[RetrievalResult, object | None]:
        """Internal packing hook: rank within an optional output scope.

        The callback receives detached inputs *before* the final witness check.
        Its return value is discarded when the witness changed, so callers must
        not retain a scanner or publish partial data.
        """

        return self._execute(
            request, allowed_paths=allowed_paths, on_ranked=on_ranked
        )

    def _execute(
        self,
        request: RetrievalRequest,
        *,
        allowed_paths: frozenset[str] | None = None,
        on_ranked: RankingCallback | None = None,
    ) -> tuple[RetrievalResult, object | None]:
        if not isinstance(request, RetrievalRequest):
            raise InvalidContextRequest("Retrieval query was rejected")
        config = self.repository.config.retrieval
        top_k = request.top_k if request.top_k is not None else config.top_k
        weights = effective_weights(config.weights, request.task_kind)
        prepared = prepare_query(request)
        degraded: list[DegradedReason] = []
        truncated = False

        try:
            scan_cm = self.repository.scan_retrieval_sources()
        except ContextScanError:
            log.warning("[Context.retriever] source unavailable")
            return self._unavailable(request, top_k=top_k), None

        try:
            with scan_cm as scan:
                if not scan.complete:
                    degraded.append("source_scan_incomplete")
                    truncated = True
                    hits, more_trunc, selected = self._rank_local(
                        scan,
                        request=request,
                        prepared=prepared,
                        weights=weights,
                        top_k=top_k,
                        graph=None,
                        git_paths=None,
                        degraded=degraded,
                        allowed_paths=allowed_paths,
                    )
                    truncated = truncated or more_trunc
                    result = RetrievalResult(
                        hits=tuple(hits),
                        task_kind=request.task_kind,
                        kind=request.kind,
                        top_k=top_k,
                        degraded_reasons=_ordered_degraded(degraded),
                        truncated=truncated,
                    )
                    outcome = (
                        on_ranked(result, scan, None, tuple(selected))
                        if on_ranked is not None
                        else None
                    )
                    if not scan.revalidate():
                        log.warning(
                            "[Context.retriever] source changed after incomplete scan"
                        )
                        return (
                            self._changed(request, top_k=top_k, degraded=degraded),
                            None,
                        )
                    log.debug(
                        "[Context.retriever] task=%s sources=%s returned=%s truncated=%s degraded=%s",
                        request.task_kind,
                        len(scan.snapshots),
                        len(result.hits),
                        truncated,
                        degraded,
                    )
                    return result, outcome

                graph = None
                if self.load_graph is not None:
                    try:
                        graph = self.load_graph(scan)
                    except CodeIndexError as exc:
                        code = _map_index_error(exc)
                        degraded.append(code)
                        log.debug("[Context.retriever] index degraded code=%s", code)
                    except Exception:
                        raise

                git_paths: frozenset[str] | None = None
                if self.read_diff is not None and weights.get("git_diff", 0) > 0:
                    admitted = [item.path for item in scan.snapshots]
                    git_paths = self.read_diff(Path(scan.policy.root), admitted)
                    if git_paths is None:
                        degraded.append("git_unavailable")
                        log.debug("[Context.retriever] git unavailable")

                batch = (
                    self._cache.begin_batch() if self._cache is not None else None
                )
                cached = None
                if self._retrieval_cache is not None:
                    cached = self._retrieval_cache.load(
                        request,
                        top_k=top_k,
                        weights=weights,
                        allowed_paths=allowed_paths,
                        scan=scan,
                        graph=graph,
                        git_paths=git_paths,
                        degraded=degraded,
                        batch=batch,
                    )
                if cached is not None:
                    result = cached["result"]
                    selected = cached["selected"]
                    outcome = (
                        on_ranked(result, scan, graph, tuple(selected))
                        if on_ranked is not None
                        else None
                    )
                    if not scan.revalidate():
                        log.warning(
                            "[Context.retriever] source changed after cached result"
                        )
                        return (
                            self._changed(request, top_k=top_k, degraded=degraded),
                            None,
                        )
                    log.debug(
                        "[Context.retriever] task=%s sources=%s returned=%s truncated=%s degraded=%s cache=hit",
                        request.task_kind,
                        len(scan.snapshots),
                        len(result.hits),
                        result.truncated,
                        degraded,
                    )
                    return result, outcome

                result_hits, more_trunc, selected = self._rank_local(
                    scan,
                    request=request,
                    prepared=prepared,
                    weights=weights,
                    top_k=top_k,
                    graph=graph,
                    git_paths=git_paths,
                    degraded=degraded,
                    allowed_paths=allowed_paths,
                    batch=batch,
                )
                truncated = truncated or more_trunc
                result = RetrievalResult(
                    hits=tuple(result_hits),
                    task_kind=request.task_kind,
                    kind=request.kind,
                    top_k=top_k,
                    degraded_reasons=_ordered_degraded(degraded),
                    truncated=truncated,
                )
                if self._retrieval_cache is not None:
                    self._retrieval_cache.publish(
                        request,
                        top_k=top_k,
                        weights=weights,
                        allowed_paths=allowed_paths,
                        scan=scan,
                        graph=graph,
                        git_paths=git_paths,
                        degraded=degraded,
                        batch=batch,
                        result=result,
                        selected=selected,
                    )
                outcome = (
                    on_ranked(result, scan, graph, tuple(selected))
                    if on_ranked is not None
                    else None
                )
                if not scan.revalidate():
                    if batch is not None:
                        batch.discard()
                    log.warning("[Context.retriever] source changed before return")
                    return (
                        self._changed(request, top_k=top_k, degraded=degraded),
                        None,
                    )
                if batch is not None and self._cache is not None:
                    self._cache.flush(batch)
                log.debug(
                    "[Context.retriever] task=%s sources=%s returned=%s truncated=%s degraded=%s",
                    request.task_kind,
                    len(scan.snapshots),
                    len(result.hits),
                    truncated,
                    degraded,
                )
                return result, outcome
        except ContextScanError:
            log.warning("[Context.retriever] source unavailable at scan entry")
            return self._unavailable(request, top_k=top_k), None

    @staticmethod
    def _unavailable(request: RetrievalRequest, *, top_k: int) -> RetrievalResult:
        return RetrievalResult(
            hits=(),
            task_kind=request.task_kind,
            kind=request.kind,
            top_k=top_k,
            degraded_reasons=("source_unavailable",),
            truncated=False,
        )

    @staticmethod
    def _changed(
        request: RetrievalRequest,
        *,
        top_k: int,
        degraded: Iterable[DegradedReason],
    ) -> RetrievalResult:
        return RetrievalResult(
            hits=(),
            task_kind=request.task_kind,
            kind=request.kind,
            top_k=top_k,
            degraded_reasons=_ordered_degraded([*degraded, "source_changed"]),
            truncated=True,
        )

    def _rank_local(
        self,
        scan: SourceScan,
        *,
        request: RetrievalRequest,
        prepared: PreparedQuery,
        weights: Mapping[SignalName, int],
        top_k: int,
        graph: "DependencyGraph | None",
        git_paths: frozenset[str] | None,
        degraded: list[DegradedReason],
        allowed_paths: frozenset[str] | None = None,
        batch=None,
    ) -> tuple[list[RetrievalHit], bool, list[RankCandidate]]:
        truncated_flag = False
        snapshots = {
            item.path: item
            for item in scan.snapshots
            if not contains_secret(item.path)
            and (allowed_paths is None or item.path in allowed_paths)
        }
        states: dict[str, _PathState] = {}
        for path, snapshot in snapshots.items():
            states[path] = _PathState(
                snapshot=snapshot,
                lines=iter_lines(snapshot.text),
                module_name=module_name_for_path(path) if is_python(path) else "",
            )

        definitions_by_path: dict[str, list[_Definition]] = {
            path: [] for path in states
        }
        if graph is not None:
            for symbol in graph.symbols:
                if symbol.source.path not in states:
                    continue
                definition = _Definition(
                    symbol_id=symbol.symbol_id,
                    path=symbol.source.path,
                    qualified_name=symbol.qualified_name,
                    simple_name=symbol.simple_name,
                    kind=symbol.kind,
                    start_line=symbol.source.start_line or 1,
                    end_line=symbol.source.end_line or (symbol.source.start_line or 1),
                    digest=symbol.source.digest,
                    source_kind="indexed_definition",
                    occurrence=symbol.occurrence,
                )
                definitions_by_path[symbol.source.path].append(definition)
                states[symbol.source.path].definitions.append(definition)
        else:
            for path, state in states.items():
                if not is_python(path):
                    continue
                facts = None
                if self._fact_cache is not None:
                    outcome = self._fact_cache.lookup(state.snapshot)
                    if outcome.hit:
                        facts = outcome.value
                peer_facts = None
                if facts is None and self.authorized_fact_peer is not None:
                    peer_facts = self.authorized_fact_peer.load(state.snapshot)
                if facts is None:
                    facts = peer_facts or extract_python_facts(state.snapshot)
                    if self._fact_cache is not None and batch is not None:
                        self._fact_cache.publish(
                            state.snapshot, facts, batch=batch
                        )
                if facts.status not in {"ok", "ok_truncated"}:
                    continue
                for item in facts.definitions:
                    symbol_id = live_symbol_id(
                        path,
                        item.local_name,
                        item.kind.value,
                        item.occurrence,
                    )
                    qualified = (
                        f"{state.module_name}.{item.local_name}"
                        if state.module_name
                        else item.local_name
                    )
                    definition = _Definition(
                        symbol_id=symbol_id,
                        path=path,
                        qualified_name=qualified,
                        simple_name=item.simple_name,
                        kind=item.kind.value,
                        start_line=item.start_line,
                        end_line=item.end_line,
                        digest=state.snapshot.digest,
                        source_kind="live_definition",
                        occurrence=item.occurrence,
                    )
                    definitions_by_path[path].append(definition)
                    state.definitions.append(definition)

        for path in definitions_by_path:
            definitions_by_path[path].sort(key=lambda item: (item.start_line, item.symbol_id))

        lines_by_path = {path: state.lines for path, state in states.items()}
        symbol_lex: dict[str, tuple[int, int, bool, LiteralLineMatch | None]] = {}
        # path -> (primary_mask, expanded_mask, phrase, best_anchor)

        def on_match(path: str, line: int, primary_mask: int, expanded_mask: int, phrase: bool) -> None:
            defs = definitions_by_path.get(path) or []
            if not defs:
                return
            # Advance through spans; choose smallest covering definition.
            covering = [
                item
                for item in defs
                if item.start_line <= line <= item.end_line
            ]
            if not covering:
                return
            covering.sort(key=lambda item: (item.end_line - item.start_line, item.start_line, item.symbol_id))
            chosen = covering[0]
            match = LiteralLineMatch(
                path=path,
                line=line,
                primary_mask=primary_mask,
                expanded_mask=expanded_mask,
                phrase=phrase,
            )
            previous = symbol_lex.get(chosen.symbol_id)
            if previous is None:
                symbol_lex[chosen.symbol_id] = (primary_mask, expanded_mask, phrase, match)
                return
            p_mask, e_mask, had_phrase, best = previous
            union_p = p_mask | primary_mask
            union_e = e_mask | expanded_mask
            union_phrase = had_phrase or phrase
            # Best anchor by Task 2 order.
            candidates = [item for item in (best, match) if item is not None]
            candidates.sort(
                key=lambda item: (
                    0 if item.phrase else 1,
                    -item.primary_mask.bit_count(),
                    -item.expanded_mask.bit_count(),
                    item.line,
                )
            )
            symbol_lex[chosen.symbol_id] = (
                union_p,
                union_e,
                union_phrase,
                candidates[0],
            )

        literal = search_snapshot_literals(
            lines_by_path,
            prepared.phrase,
            prepared.primary,
            prepared.expanded,
            on_match=on_match,
        )
        if literal.degraded_reason is not None:
            degraded.append("lexical_python_fallback")
        if literal.anchors_capped or prepared.primary_capped or prepared.expanded_capped:
            truncated_flag = True

        file_lex = {item.path: item for item in literal.files}
        for path, evidence in file_lex.items():
            if path in states:
                states[path].lexical = evidence

        # Graph indexes
        path_to_id: dict[str, str] = {}
        id_to_path: dict[str, str] = {}
        outgoing_paths: dict[str, list[str]] = {}
        incoming_paths: dict[str, list[str]] = {}
        incoming_counts: dict[str, int] = {}
        outgoing_counts: dict[str, int] = {}
        relation_rows: list[tuple[str, str, DependencyEvidence]] = []
        if graph is not None:
            for node in graph.nodes:
                path_to_id[node.path] = node.file_id
                id_to_path[node.file_id] = node.path
            for file_id, targets in graph.forward.items():
                path = id_to_path.get(file_id)
                if path is None:
                    continue
                outs = [
                    id_to_path[item]
                    for item in targets
                    if item in id_to_path and item != file_id
                ]
                outgoing_paths[path] = sorted(set(outs))
                outgoing_counts[path] = len(set(outs))
            for file_id, sources in graph.reverse.items():
                path = id_to_path.get(file_id)
                if path is None:
                    continue
                inns = [
                    id_to_path[item]
                    for item in sources
                    if item in id_to_path and item != file_id
                ]
                incoming_paths[path] = sorted(set(inns))
                incoming_counts[path] = len(set(inns))
            for relation in graph.relations:
                importer = id_to_path.get(relation.importer_id)
                target = id_to_path.get(relation.target_id or "")
                if importer is None or target is None:
                    continue
                evidence = DependencyEvidence(
                    direction="import",
                    related_path=target,
                    importer_path=importer,
                    start_line=relation.source.start_line or 1,
                    end_line=relation.source.end_line or 1,
                    source_digest=relation.source.digest,
                )
                relation_rows.append((importer, target, evidence))
                reverse = DependencyEvidence(
                    direction="reverse",
                    related_path=importer,
                    importer_path=importer,
                    start_line=relation.source.start_line or 1,
                    end_line=relation.source.end_line or 1,
                    source_digest=relation.source.digest,
                )
                relation_rows.append((target, importer, reverse))

        eligible = set(states)
        candidates: dict[tuple[str, str], RankCandidate] = {}

        def ensure_file(path: str) -> RankCandidate:
            key = ("file", path)
            if key not in candidates:
                candidates[key] = RankCandidate(
                    kind="file",
                    path=path,
                    source_digest=states[path].snapshot.digest,
                )
            return candidates[key]

        def ensure_symbol(definition: _Definition) -> RankCandidate:
            key = ("symbol", definition.symbol_id)
            if key not in candidates:
                candidates[key] = RankCandidate(
                    kind="symbol",
                    path=definition.path,
                    symbol_id=definition.symbol_id,
                    qualified_name=definition.qualified_name,
                    definition_start=definition.start_line,
                    definition_end=definition.end_line,
                    source_digest=definition.digest,
                    source_kind=definition.source_kind,  # type: ignore[arg-type]
                )
            return candidates[key]

        # Score files and symbols for base signals
        for path, state in states.items():
            cached_base = None
            if self._candidate_cache is not None:
                cached_base = self._candidate_cache.load(
                    path=path,
                    snapshot=state.snapshot,
                    definitions=definitions_by_path[path],
                    lexical=state.lexical,
                    prepared=prepared,
                    batch=batch,
                )
            if cached_base is not None:
                candidates[("file", path)] = cached_base["file"]
                for symbol in cached_base["symbols"]:
                    candidates[("symbol", symbol.symbol_id)] = symbol
                continue
            file_cand = ensure_file(path)
            lex = state.lexical
            if lex is not None:
                file_cand._primary_mask = lex.primary_mask
                file_cand._expanded_mask = lex.expanded_mask
                file_cand._phrase = lex.phrase
                if lex.anchors:
                    file_cand._anchor = lex.anchors[0]
                file_cand.signals["lexical"] = lexical_score(
                    phrase=lex.phrase,
                    primary_mask=lex.primary_mask,
                    expanded_mask=lex.expanded_mask,
                    primary_count=len(prepared.primary),
                    expanded_count=len(prepared.expanded),
                )
            file_cand.signals["name"] = name_score_for_file(
                path,
                prepared.primary,
                module_name=state.module_name,
            )
            # File exact_symbol = strongest definition in file
            best_exact = 0
            for definition in state.definitions:
                score = exact_symbol_score(
                    definition.qualified_name,
                    definition.simple_name,
                    prepared.selectors,
                )
                best_exact = max(best_exact, score)
                symbol = ensure_symbol(definition)
                symbol.signals["exact_symbol"] = score
                symbol.signals["name"] = name_score_for_symbol(
                    definition.qualified_name,
                    definition.simple_name,
                    prepared.primary,
                )
                lex_state = symbol_lex.get(definition.symbol_id)
                if lex_state is not None:
                    p_mask, e_mask, phrase, anchor = lex_state
                    symbol._primary_mask = p_mask
                    symbol._expanded_mask = e_mask
                    symbol._phrase = phrase
                    symbol._anchor = anchor
                    symbol.signals["lexical"] = lexical_score(
                        phrase=phrase,
                        primary_mask=p_mask,
                        expanded_mask=e_mask,
                        primary_count=len(prepared.primary),
                        expanded_count=len(prepared.expanded),
                    )
            file_cand.signals["exact_symbol"] = best_exact
            if self._candidate_cache is not None and batch is not None:
                self._candidate_cache.publish(
                    path=path,
                    snapshot=state.snapshot,
                    definitions=definitions_by_path[path],
                    lexical=state.lexical,
                    prepared=prepared,
                    batch=batch,
                    value={
                        "file": file_cand,
                        "symbols": [
                            candidates[("symbol", definition.symbol_id)]
                            for definition in state.definitions
                            if ("symbol", definition.symbol_id) in candidates
                        ],
                    },
                )

        # Direct seeds
        direct_files = [
            cand
            for cand in candidates.values()
            if cand.kind == "file" and is_direct_candidate(cand.signals, weights)
        ]
        direct_files.sort(
            key=lambda item: (-direct_weighted_points(item.signals, weights), item.path)
        )
        if len(direct_files) > 8:
            truncated_flag = True
        seeds = [item.path for item in direct_files[:8]]

        # One-hop expansion
        out_paths: list[str] = []
        in_paths: list[str] = []
        if graph is not None and seeds:
            if weights["import_dependency"] > 0 or weights["reverse_dependency"] > 0:
                for seed in seeds:
                    if weights["import_dependency"] > 0:
                        outs = [p for p in outgoing_paths.get(seed, []) if p in eligible][:8]
                        if len(outgoing_paths.get(seed, [])) > 8:
                            truncated_flag = True
                        out_paths.extend(outs)
                    if weights["reverse_dependency"] > 0:
                        inns = [p for p in incoming_paths.get(seed, []) if p in eligible][:8]
                        if len(incoming_paths.get(seed, [])) > 8:
                            truncated_flag = True
                        in_paths.extend(inns)
                for path in sorted(set(out_paths)):
                    cand = ensure_file(path)
                    cand.signals["import_dependency"] = 1000
                for path in sorted(set(in_paths)):
                    cand = ensure_file(path)
                    cand.signals["reverse_dependency"] = 1000

            # Apply import/reverse bonuses to independently matched symbols only
            for cand in list(candidates.values()):
                if cand.kind != "symbol":
                    continue
                if not is_direct_candidate(cand.signals, weights):
                    continue
                if cand.path in set(out_paths):
                    cand.signals["import_dependency"] = 1000
                if cand.path in set(in_paths):
                    cand.signals["reverse_dependency"] = 1000

        # Related tests
        if weights["related_test"] > 0 and seeds:
            tests, overflow = bound_matching_tests(eligible, seeds)
            if overflow:
                truncated_flag = True
            for path in tests:
                cand = ensure_file(path)
                cand.signals["related_test"] = 1000
                # Boost independently matched symbols in those files
            for cand in list(candidates.values()):
                if cand.kind == "symbol" and cand.path in tests and is_direct_candidate(cand.signals, weights):
                    cand.signals["related_test"] = 1000

        # Git + map importance boosts only for independently matched candidates
        qualified_paths = {
            cand.path
            for cand in candidates.values()
            if is_direct_candidate(cand.signals, weights)
            or cand.signals.get("import_dependency")
            or cand.signals.get("reverse_dependency")
            or cand.signals.get("related_test")
        }
        if git_paths is not None and weights["git_diff"] > 0:
            for path in git_paths:
                if path not in qualified_paths:
                    continue
                for cand in candidates.values():
                    if cand.path != path:
                        continue
                    if cand.kind == "file" or is_direct_candidate(cand.signals, weights):
                        cand.signals["git_diff"] = 1000
        if graph is not None and weights["map_importance"] > 0:
            for cand in candidates.values():
                if cand.path not in qualified_paths:
                    continue
                if cand.kind == "symbol" and not is_direct_candidate(cand.signals, weights):
                    continue
                score = map_importance_score(
                    incoming_counts.get(cand.path, 0),
                    outgoing_counts.get(cand.path, 0),
                )
                if score > 0:
                    cand.signals["map_importance"] = score

        # Drop non-qualifying candidates (need direct or expansion signal with positive weight)
        kept: list[RankCandidate] = []
        for cand in candidates.values():
            if is_direct_candidate(cand.signals, weights):
                kept.append(cand)
                continue
            bonus = False
            for name in ("import_dependency", "reverse_dependency", "related_test"):
                if weighted_points(cand.signals.get(name, 0), weights[name]) > 0:
                    bonus = True
                    break
            if bonus and cand.kind == "file":
                kept.append(cand)
        candidates_list = kept

        # Dependency evidence (up to 8)
        seed_set = set(seeds)
        for cand in candidates_list:
            related: list[DependencyEvidence] = []
            for importer, target, evidence in relation_rows:
                if cand.path not in {importer, target}:
                    continue
                if evidence.direction == "import" and importer != cand.path:
                    continue
                if evidence.direction == "reverse" and target != cand.path:
                    # reverse rows were keyed as (target, importer, reverse)
                    continue
                # Prefer links to/from seeds
                related.append(evidence)
            related.sort(
                key=lambda item: (
                    0
                    if item.related_path in seed_set or item.importer_path in seed_set
                    else 1,
                    item.direction,
                    item.related_path,
                    item.importer_path,
                    item.start_line,
                    item.end_line,
                )
            )
            # Unique by tuple fields
            unique: list[DependencyEvidence] = []
            seen: set[tuple] = set()
            for item in related:
                key = (
                    item.direction,
                    item.related_path,
                    item.importer_path,
                    item.start_line,
                    item.end_line,
                    item.source_digest,
                )
                if key in seen:
                    continue
                seen.add(key)
                unique.append(item)
                if len(unique) >= 8:
                    if len(related) > 8:
                        truncated_flag = True
                    break
            cand.related = unique

        # Windows and costs
        char_cap = min(12000, self.repository.config.per_file_chars_max)
        for cand in candidates_list:
            state = states[cand.path]
            empty = len(state.lines) == 0
            if cand.kind == "file":
                lexical_positive = weighted_points(cand.signals["lexical"], weights["lexical"]) > 0
                start, end, source_kind, text = select_file_window(
                    state.lines,
                    empty=empty,
                    lexical_positive=lexical_positive,
                    anchor=cand._anchor,
                    char_cap=char_cap,
                )
                cand.start_line = start
                cand.end_line = end
                cand.source_kind = source_kind  # type: ignore[assignment]
                cand.estimated_token_cost = estimate_window_cost(text, empty=empty)
            else:
                assert cand.definition_start is not None and cand.definition_end is not None
                exact_or_name = (
                    weighted_points(cand.signals["exact_symbol"], weights["exact_symbol"]) > 0
                    or weighted_points(cand.signals["name"], weights["name"]) > 0
                )
                start, end, text = select_definition_window(
                    state.lines,
                    definition_start=cand.definition_start,
                    definition_end=cand.definition_end,
                    exact_or_name=exact_or_name,
                    anchor=cand._anchor,
                    char_cap=char_cap,
                )
                cand.start_line = start
                cand.end_line = end
                cand.estimated_token_cost = estimate_window_cost(text, empty=False)
            cand._unrounded_points = sum(
                weighted_points(cand.signals.get(name, 0), weights[name])
                for name in SIGNAL_ORDER
            )

        candidates_list.sort(key=lambda item: sort_key(item, weights))
        selected, select_trunc = dedupe_and_select(
            candidates_list, kind=request.kind, top_k=top_k
        )
        truncated_flag = truncated_flag or select_trunc

        hits: list[RetrievalHit] = []
        for cand in selected:
            if contains_secret(cand.path) or (
                cand.qualified_name and contains_secret(cand.qualified_name)
            ):
                continue
            hits.append(
                RetrievalHit(
                    kind=cand.kind,
                    path=cand.path,
                    symbol_id=cand.symbol_id,
                    qualified_name=cand.qualified_name,
                    definition_start_line=cand.definition_start if cand.kind == "symbol" else None,
                    start_line=cand.start_line,
                    end_line=cand.end_line,
                    source_digest=cand.source_digest,
                    source_kind=cand.source_kind,
                    relevance_score=relevance_score(cand.signals, weights),
                    ranking_reasons=ranking_reasons(cand.signals, weights),
                    related_dependencies=tuple(cand.related),
                    estimated_token_cost=cand.estimated_token_cost,
                )
            )
        return hits, truncated_flag, selected
