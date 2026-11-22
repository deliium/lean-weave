"""Token-aware, revision-checked context packing (V2).

The packer is a read-only opt-in composition surface.  It never changes
``ContextPack``, ``ContextProvider`` or workflow stage behavior, and it never
calls a model or maintains the code index.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from functools import cmp_to_key
from typing import Any, Callable, Mapping, Sequence

from lean_weave.context.models import iter_lines
from lean_weave.context.policy import is_python
from lean_weave.context.packing_models import (
    CandidateRef,
    ExpansionDelivery,
    ExpansionRequest,
    FragmentRef,
    PackEnvelope,
    PackMetrics,
    PackOmission,
    PackRequest,
    PackResult,
    PackStatus,
    PackedContextV2,
    PackedFragment,
    PackedMapRow,
    REASON_CUMULATIVE_BUDGET,
    REASON_DEPENDENCY_UNAVAILABLE,
    REASON_EXPANSION_LIMIT,
    REASON_INVALID_STRUCTURAL_RANGE,
    REASON_MAP_UNAVAILABLE,
    REASON_NO_RELEVANT_SOURCE,
    REASON_OVERLAP_REQUIRES_EXPLICIT_REBUILD,
    REASON_REQUIRED_SOURCE_OVER_BUDGET,
    REASON_REQUIRED_SOURCE_UNAVAILABLE,
    REASON_SECTION_BUDGET,
    REASON_SOURCE_CHANGED,
    REASON_SOURCE_SCAN_INCOMPLETE,
    REASON_SOURCE_UNAVAILABLE,
    REASON_TOTAL_BUDGET,
    SourceRevision,
)
from lean_weave.context.packing_session import PackingSession, TaskProjection
from lean_weave.context.packing_source import (
    PackingCatalog,
    SourceUnit,
    candidate_ref,
    map_rows_from_graph,
    metadata_map_rows,
    relation_evidence,
    slice_lines,
    unit_overlap,
)
from lean_weave.context.packing_tokens import (
    ConservativeByteCounter,
    TokenCounter,
    TokenMeasurement,
    measure,
    render_source,
    supports_counter,
    tool_section_text,
)
from lean_weave.context.repository import FileSnapshot, Repository, SourceScan
from lean_weave.context_cache.facts import FactCache
from lean_weave.context_cache.map_rows import MapRowCache
from lean_weave.context_cache.models import ContextCache
from lean_weave.context_cache.packing import PackingSpanCache
from lean_weave.context_cache.service import CachePolicy
from lean_weave.context.retrieval_models import (
    RetrievalHit,
    RetrievalRequest,
    RetrievalResult,
)
from lean_weave.context.retrieval_ranking import RankCandidate
from lean_weave.context.retriever import ContextRetriever
from lean_weave.contracts.deepseek_request import (
    normalize_chat_request,
    normalize_tool_calls,
    project_wire,
    wire_fingerprint,
)
from lean_weave.errors import (
    CodeIndexError,
    ContextScanError,
    InvalidContextRequest,
    UnsupportedLLMRequest,
)
from lean_weave.logging_setup import get_logger
from lean_weave.repository_map.models import DependencyGraph

log = get_logger("context.packer")

_DIRECT_SIGNALS = frozenset({"exact_symbol", "name", "lexical"})
_EVIDENCE_SIGNALS = frozenset(
    {"exact_symbol", "name", "lexical", "import_dependency", "reverse_dependency"}
)


@dataclass
class _CounterState:
    adapter: TokenCounter | None
    failed: bool = False


@dataclass(frozen=True)
class _Assembly:
    sections: dict[str, str]
    messages: tuple[dict[str, Any], ...]
    wire: dict[str, Any]
    measurement: TokenMeasurement


@dataclass(frozen=True)
class Composition:
    result: PackResult
    revisions: Mapping[str, str] = field(default_factory=dict)
    fragments: tuple[PackedFragment, ...] = ()
    candidates: tuple[CandidateRef, ...] = ()
    relations: tuple[Any, ...] = ()
    policy: Any = None
    root_binding: tuple[str, int, int] | None = None
    cumulative_units: int = 0
    model: str = ""
    thinking: str = "disabled"
    output_tokens: int = 0


class ContextPacker:
    """Deterministic L0/L1 composition plus bounded explicit L2 expansion."""

    def __init__(
        self,
        repository: Repository,
        *,
        retriever: ContextRetriever | None = None,
        token_counter: TokenCounter | None = None,
        model: str = "deepseek-flash",
        thinking: bool = False,
        cache: ContextCache | None = None,
        workflow_tool_tokens: int | None = None,
        rank_observer: Callable[[Any], None] | None = None,
    ) -> None:
        self.repository = repository
        self.retriever = retriever or ContextRetriever(repository)
        self.token_counter = token_counter
        self.packing_config = repository.config.packing
        self.context_config = repository.config
        self.default_model = model
        self.default_thinking = thinking
        self._workflow_tool_tokens = workflow_tool_tokens
        self._rank_observer = rank_observer
        self._cache = cache if cache is not None and cache.enabled else None
        self._fact_cache = None
        self._span_cache = None
        self._row_cache = None
        if self._cache is not None:
            policy = CachePolicy.from_context(
                repository.config,
                state_dir=repository.state_dir,
                protected_paths=repository.protected_paths,
            )
            self._fact_cache = FactCache(self._cache, policy=policy)
            self._span_cache = PackingSpanCache(self._cache, policy=policy)
            self._row_cache = MapRowCache(self._cache, policy=policy)

    # -- public initial composition ---------------------------------
    def build(self, request: PackRequest) -> PackResult:
        result, _outcome = self._compose(request)
        return result

    def compose_scoped(
        self,
        request: PackRequest,
        *,
        scan_paths: frozenset[str] | None,
        read_filter=None,
        graph: DependencyGraph | None = None,
        output_cap: int | None = None,
    ) -> tuple[PackResult, Composition | None]:
        """Compose without widening a workflow's frozen read/emit scope."""

        return self._compose(
            request,
            scan_paths=scan_paths,
            read_filter=read_filter,
            graph=graph,
            output_cap=output_cap,
        )

    def open_scoped(
        self,
        request: PackRequest,
        *,
        scan_paths: frozenset[str] | None,
        read_filter=None,
        graph: DependencyGraph | None = None,
        output_cap: int | None = None,
    ) -> tuple[PackResult, Composition | None, PackingSession | None]:
        result, outcome = self._compose(
            request,
            scan_paths=scan_paths,
            read_filter=read_filter,
            graph=graph,
            output_cap=output_cap,
        )
        if result.status != "ready" or outcome is None:
            return result, outcome, None
        session = self._session_from_composition(request, outcome)
        return result, outcome, session

    def session_from_composition(
        self, request: PackRequest, outcome: Composition
    ) -> PackingSession:
        return self._session_from_composition(request, outcome)

    def _session_from_composition(
        self, request: PackRequest, outcome: Composition
    ) -> PackingSession:
        if outcome.root_binding is None or outcome.policy is None:
            raise InvalidContextRequest("A scoped composition is required")
        coverage: dict[str, tuple[tuple[int, int], ...]] = {}
        for fragment in outcome.fragments:
            key = f"{fragment.path}@{fragment.digest}"
            coverage[key] = (*coverage.get(key, ()), (fragment.start_line, fragment.end_line))
        return PackingSession(
            task=TaskProjection(
                task_id=request.task_id,
                query=request.query,
                task_specification=request.task_specification,
                system_instructions=request.system_instructions,
                level=request.level,
                output_paths=request.output_paths,
            ),
            config=self.packing_config,
            model=outcome.model,
            thinking=outcome.thinking,
            output_tokens=outcome.output_tokens,
            root_binding=outcome.root_binding,
            policy=outcome.policy,
            revisions=outcome.revisions,
            fragments={
                fragment.fragment_id: FragmentRef(
                    fragment_id=fragment.fragment_id,
                    path=fragment.path,
                    start_line=fragment.start_line,
                    end_line=fragment.end_line,
                    digest=fragment.digest,
                    kind=fragment.kind,
                )
                for fragment in outcome.fragments
            },
            coverage=coverage,
            candidates=outcome.candidates,
            relations=tuple(outcome.relations),
            cumulative_units=outcome.cumulative_units,
            file_chars={
                fragment.path: sum(
                    len(item.text) for item in outcome.fragments if item.path == fragment.path
                )
                for fragment in outcome.fragments
            },
        )

    def open_task(
        self, request: PackRequest
    ) -> tuple[PackResult, PackingSession | None]:
        result, outcome = self._compose(request)
        if result.status != "ready" or outcome is None:
            return result, None
        if outcome.root_binding is None or outcome.policy is None:
            return result, None
        coverage: dict[str, tuple[tuple[int, int], ...]] = {}
        for fragment in outcome.fragments:
            key = f"{fragment.path}@{fragment.digest}"
            coverage[key] = (*coverage.get(key, ()), (fragment.start_line, fragment.end_line))
        session = PackingSession(
            task=TaskProjection(
                task_id=request.task_id,
                query=request.query,
                task_specification=request.task_specification,
                system_instructions=request.system_instructions,
                level=request.level,
                output_paths=request.output_paths,
            ),
            config=self.packing_config,
            model=outcome.model,
            thinking=outcome.thinking,
            output_tokens=outcome.output_tokens,
            root_binding=outcome.root_binding,
            policy=outcome.policy,
            revisions=outcome.revisions,
            fragments={
                fragment.fragment_id: FragmentRef(
                    fragment_id=fragment.fragment_id,
                    path=fragment.path,
                    start_line=fragment.start_line,
                    end_line=fragment.end_line,
                    digest=fragment.digest,
                    kind=fragment.kind,
                )
                for fragment in outcome.fragments
            },
            coverage=coverage,
            candidates=outcome.candidates,
            relations=tuple(outcome.relations),
            cumulative_units=outcome.cumulative_units,
            file_chars={
                fragment.path: sum(
                    len(item.text) for item in outcome.fragments if item.path == fragment.path
                )
                for fragment in outcome.fragments
            },
        )
        log.debug(
            "[Context.packer] session_open fragments=%s revisions=%s cumulative=%s",
            len(session.fragments),
            len(session.revisions),
            session.cumulative_units,
        )
        return result, session

    # -- initial composition ----------------------------------------
    def _compose(
        self,
        request: PackRequest,
        *,
        scan_paths: frozenset[str] | None = None,
        read_filter=None,
        graph: DependencyGraph | None = None,
        output_cap: int | None = None,
    ) -> tuple[PackResult, Composition | None]:
        if not isinstance(request, PackRequest):
            raise InvalidContextRequest("A packed context request is required")
        scope = (
            frozenset(request.output_paths)
            if request.output_paths is not None
            else None
        )
        effective_output = self._effective_output_tokens(request, output_cap)
        counter_state = _CounterState(self.token_counter)
        try:
            envelope = self._normalize_envelope(request.envelope, output_tokens=effective_output)
        except UnsupportedLLMRequest:
            raise InvalidContextRequest("Unsupported packing envelope") from None

        preflight = self._assemble(
            system=request.system_instructions,
            task=request.task_specification,
            units=(),
            rows=(),
            model=envelope.model,
            thinking=envelope.thinking,
            output_tokens=effective_output,
            tools=envelope.tools,
            replay=envelope.replay,
            response_format=envelope.response_format,
            counter_state=counter_state,
            revisions=request.editable_revisions,
        )
        failing = self._section_failures(preflight.measurement)
        if failing or preflight.measurement.total_tokens > self.packing_config.total_tokens:
            reasons = tuple(f"{REASON_SECTION_BUDGET}:{name}" for name in failing)
            if not reasons:
                reasons = (REASON_TOTAL_BUDGET,)
            return (
                self._failure(
                    request,
                    status="insufficient_context",
                    reasons=reasons,
                    omissions=tuple(
                        PackOmission(reason=f"section_budget:{name}") for name in failing
                    ),
                    measurement=preflight.measurement,
                    model=envelope.model,
                ),
                None,
            )

        batch = self._cache.begin_batch() if self._cache is not None else None
        try:
            if scan_paths is None and read_filter is None and graph is None:
                result, outcome = self.retriever.rank_checked(
                    RetrievalRequest(query=request.query),
                    allowed_paths=scope,
                    on_ranked=lambda ranked, scan, ranked_graph, selected: self._observed_select(
                        ranked,
                        request,
                        scan,
                        ranked_graph,
                        selected,
                        envelope,
                        counter_state,
                        batch,
                        effective_output,
                    ),
                )
            else:
                result, outcome = self.retriever.rank_checked(
                    RetrievalRequest(query=request.query),
                    allowed_paths=scope,
                    scan_paths=scan_paths,
                    read_filter=read_filter,
                    graph=graph,
                    on_ranked=lambda ranked, scan, ranked_graph, selected: self._observed_select(
                        ranked,
                        request,
                        scan,
                        ranked_graph,
                        selected,
                        envelope,
                        counter_state,
                        batch,
                        effective_output,
                    ),
                )
        except ContextScanError:
            return (
                self._failure(
                    request,
                    status="insufficient_context",
                    reasons=(REASON_SOURCE_UNAVAILABLE,),
                    omissions=(PackOmission(reason=REASON_SOURCE_UNAVAILABLE),),
                    measurement=preflight.measurement,
                    model=envelope.model,
                ),
                None,
            )

        if outcome is None:
            if "source_changed" in result.degraded_reasons:
                return (
                    self._failure(
                        request,
                        status="stale_reference",
                        reasons=(REASON_SOURCE_CHANGED,),
                        omissions=(PackOmission(reason=REASON_SOURCE_CHANGED),),
                        measurement=preflight.measurement,
                        model=envelope.model,
                    ),
                    None,
                )
            return (
                self._failure(
                    request,
                    status="insufficient_context",
                    reasons=(REASON_SOURCE_UNAVAILABLE,),
                    omissions=(PackOmission(reason=REASON_SOURCE_UNAVAILABLE),),
                    measurement=preflight.measurement,
                    model=envelope.model,
                ),
                None,
            )
        assert isinstance(outcome, Composition)
        if batch is not None and self._cache is not None:
            if outcome.result.status == "ready":
                self._cache.flush(batch)
            else:
                batch.discard()
        return outcome.result, outcome

    @dataclass(frozen=True)
    class _EnvelopeView:
        model: str
        thinking: str
        tools: tuple[dict[str, Any], ...]
        replay: tuple[dict[str, Any], ...]
        response_format: dict[str, Any] | None
        output_tokens: int = 0

    def _normalize_envelope(
        self, envelope: PackEnvelope, *, output_tokens: int | None = None
    ) -> "_EnvelopeView":
        selected_output = (
            self.packing_config.output_tokens if output_tokens is None else output_tokens
        )
        model = envelope.model or self.default_model
        thinking = envelope.thinking or (
            "enabled" if self.default_thinking else "disabled"
        )
        self._validate_replay(envelope.replay, thinking)
        # Validate the envelope itself before any source scan.
        normalize_chat_request(
            model=model,
            messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "task"},
            ],
            tools=envelope.tools or None,
            response_format=envelope.response_format,
            thinking=thinking,  # type: ignore[arg-type]
            output_cap=selected_output,
            max_output_tokens=None,
        )
        return self._EnvelopeView(
            model=model,
            thinking=thinking,
            tools=envelope.tools,
            replay=envelope.replay,
            response_format=envelope.response_format,
            output_tokens=selected_output,
        )

    def _effective_output_tokens(
        self, request: PackRequest, output_cap: int | None
    ) -> int:
        candidates = [
            self.packing_config.output_tokens,
            request.output_tokens,
            output_cap,
        ]
        bounded = [value for value in candidates if value is not None]
        return max(1, min(bounded))

    @staticmethod
    def _validate_replay(replay: Sequence[Mapping[str, Any]], thinking: str) -> None:
        if not replay:
            return
        pending: set[str] = set()
        seen_calls: set[str] = set()
        seen_results: set[str] = set()
        for message in replay:
            role = message.get("role")
            if role == "assistant":
                calls = message.get("tool_calls")
                if not calls:
                    raise UnsupportedLLMRequest(
                        "Replay assistant messages must carry tool calls"
                    )
                normalized = normalize_tool_calls(calls)
                for call in normalized:
                    call_id = call["id"]
                    if call_id in seen_calls:
                        raise UnsupportedLLMRequest("Replay tool call IDs must be unique")
                    seen_calls.add(call_id)
                    pending.add(call_id)
                if thinking == "enabled" and not isinstance(
                    message.get("reasoning_content"), str
                ):
                    raise UnsupportedLLMRequest(
                        "Thinking replay requires assistant reasoning content"
                    )
            elif role == "tool":
                call_id = message.get("tool_call_id")
                if not isinstance(call_id, str) or not call_id:
                    raise UnsupportedLLMRequest("Replay tool results require a call ID")
                if call_id in seen_results:
                    raise UnsupportedLLMRequest("Replay tool results must be unique")
                if call_id not in pending:
                    raise UnsupportedLLMRequest(
                        "Replay tool result has no matching assistant call"
                    )
                seen_results.add(call_id)
                pending.discard(call_id)
            else:
                raise UnsupportedLLMRequest(
                    "Replay messages must be assistant or tool messages"
                )
        if pending:
            raise UnsupportedLLMRequest("Replay tool calls require matching results")

    def _observed_select(
        self,
        ranked: Any,
        request: PackRequest,
        scan: Any,
        ranked_graph: Any,
        selected: Any,
        envelope: Any,
        counter_state: Any,
        batch: Any,
        output_tokens: int,
    ) -> Any:
        if self._rank_observer is not None:
            try:
                self._rank_observer(ranked)
            except Exception:  # noqa: BLE001 - evaluation observer is non-authorizing
                pass
        return self._select(
            request,
            ranked,
            scan,
            ranked_graph,
            selected,
            envelope=envelope,
            counter_state=counter_state,
            batch=batch,
            output_tokens=output_tokens,
        )

    def _assemble(
        self,
        *,
        system: str,
        task: str,
        units: Sequence[SourceUnit],
        rows: Sequence[Mapping[str, str]],
        model: str,
        thinking: str,
        output_tokens: int,
        tools: Sequence[Mapping[str, Any]],
        replay: Sequence[Mapping[str, Any]],
        response_format: Mapping[str, Any] | None,
        counter_state: _CounterState,
        revisions: Sequence[tuple[str, str]] = (),
    ) -> _Assembly:
        task_block = f"<task_specification>\n{task}\n</task_specification>"
        map_block = (
            "<repository_map>\n"
            + "\n".join(str(row["row"]) for row in rows)
            + "\n</repository_map>"
            if rows
            else ""
        )
        revision_block = (
            "<editable_revisions>\n"
            + "\n".join(f"{path} {revision}" for path, revision in revisions)
            + "\n</editable_revisions>"
            if revisions
            else ""
        )
        source_block = (
            "<source_fragments>\n"
            + render_source(units)
            + (f"\n{revision_block}" if revision_block else "")
            + "\n</source_fragments>"
            if units or revision_block
            else ""
        )
        user_content = "\n".join(
            block for block in (task_block, map_block, source_block) if block
        )
        replay_messages = [dict(message) for message in replay]
        messages = [
            {"role": "system", "content": system},
            *replay_messages,
            {"role": "user", "content": user_content},
        ]
        normalized = normalize_chat_request(
            model=model,
            messages=messages,
            tools=tools or None,
            response_format=dict(response_format) if response_format else None,
            thinking=thinking,  # type: ignore[arg-type]
            output_cap=output_tokens,
            max_output_tokens=None,
        )
        wire = project_wire(normalized)
        sections = {
            "system": system,
            "task": task_block,
            "map": map_block,
            "source": source_block,
            "tool": tool_section_text(
                tools=tools or None,
                replay=replay,
                response_format=response_format,
            ),
        }
        counter: TokenCounter
        if (
            counter_state.adapter is not None
            and not counter_state.failed
            and supports_counter(
                counter_state.adapter,
                model=normalized.model,
                messages=wire["messages"],
                tools=wire.get("tools"),
                response_format=wire.get("response_format"),
                extra_body=wire.get("extra_body"),
            )
        ):
            counter = counter_state.adapter
        else:
            counter = ConservativeByteCounter()
        try:
            measurement = measure(
                counter,
                sections=sections,
                wire=wire,
                output_tokens=output_tokens,
                total_limit=self.packing_config.total_tokens,
            )
        except Exception:
            counter_state.failed = True
            log.debug("[Context.packer] counter_fallback=adapter_failure")
            measurement = measure(
                ConservativeByteCounter(),
                sections=sections,
                wire=wire,
                output_tokens=output_tokens,
                total_limit=self.packing_config.total_tokens,
            )
        return _Assembly(
            sections=sections,
            messages=tuple(messages),
            wire=wire,
            measurement=measurement,
        )

    def _section_limits(self) -> dict[str, int]:
        tool_tokens = self.packing_config.tool_tokens
        if self._workflow_tool_tokens is not None:
            available = max(
                0,
                self.packing_config.total_tokens - self.packing_config.output_tokens,
            )
            tool_tokens = max(tool_tokens, min(self._workflow_tool_tokens, available))
        return {
            "system": self.packing_config.system_tokens,
            "task": self.packing_config.task_tokens,
            "map": min(
                self.packing_config.map_tokens, self.context_config.map_tokens_max
            ),
            "source": self.packing_config.source_tokens,
            "tool": tool_tokens,
        }

    def _section_failures(self, measurement: TokenMeasurement) -> tuple[str, ...]:
        limits = self._section_limits()
        return tuple(
            name
            for name in ("system", "task", "map", "source", "tool")
            if measurement.section_tokens[name] > limits[name]
        )

    # -- selection ---------------------------------------------------
    def _select(
        self,
        request: PackRequest,
        ranked: RetrievalResult,
        scan: SourceScan,
        graph: DependencyGraph | None,
        selected_candidates: Sequence[RankCandidate],
        *,
        envelope: "_EnvelopeView",
        counter_state: _CounterState,
        batch=None,
        output_tokens: int | None = None,
    ) -> Composition:
        selected_output = (
            self.packing_config.output_tokens if output_tokens is None else output_tokens
        )
        scope = (
            frozenset(request.output_paths)
            if request.output_paths is not None
            else None
        )
        catalog = PackingCatalog(
            scan,
            allowed_paths=scope,
            fact_cache=self._fact_cache,
            span_cache=self._span_cache,
            batch=batch,
        )
        omitted: list[PackOmission] = []
        deduplicated = 0
        considered = len(ranked.hits)
        candidate_by_symbol = {
            candidate.symbol_id: candidate
            for candidate in selected_candidates
            if candidate.symbol_id
        }
        candidate_by_path = {
            candidate.path: candidate for candidate in selected_candidates
        }
        ranked_paths = tuple(dict.fromkeys(hit.path for hit in ranked.hits))

        required: list[SourceUnit] = []
        if request.level == "l1":
            if request.required_paths or request.forced_fragments:
                for path in request.required_paths:
                    if scope is not None and path not in scope:
                        return self._select_failure(
                            request,
                            REASON_REQUIRED_SOURCE_UNAVAILABLE,
                            "required_path_outside_scope",
                            envelope,
                        )
                    unit = catalog.complete_unit_for_path(path)
                    if unit is None:
                        return self._select_failure(
                            request,
                            REASON_REQUIRED_SOURCE_UNAVAILABLE,
                            "required_target",
                            envelope,
                        )
                    required.append(unit)
                if request.forced_fragments:
                    forced_units: list[SourceUnit] = []
                    for ref in request.forced_fragments:
                        forced = self._rematerialize(catalog, [ref], ref.kind)
                        if forced is None:
                            return self._select_failure(
                                request,
                                REASON_SOURCE_CHANGED,
                                "forced_fragment_stale",
                                envelope,
                            )
                        forced_units.extend(forced)
                    required.extend(forced_units)
            elif request.required_symbols:
                for selector in request.required_symbols:
                    if scope is not None and selector.path not in scope:
                        return self._select_failure(
                            request,
                            REASON_REQUIRED_SOURCE_UNAVAILABLE,
                            "selector_outside_scope",
                            envelope,
                        )
                    unit, reason = catalog.symbol_unit(
                        selector.path,
                        selector.qualified_name,
                        occurrence=selector.occurrence,
                        reasons=("explicit_required_symbol",),
                    )
                    if unit is None:
                        return self._select_failure(
                            request,
                            reason or REASON_REQUIRED_SOURCE_UNAVAILABLE,
                            "required_symbol",
                            envelope,
                        )
                    required.append(unit)
            else:
                anchor_hit, anchor_candidate = self._required_anchor(
                    ranked, candidate_by_symbol, candidate_by_path, catalog
                )
                if anchor_hit is None:
                    return self._select_failure(
                        request,
                        REASON_NO_RELEVANT_SOURCE,
                        "no_direct_hit",
                        envelope,
                    )
                unit, reason = catalog.anchor_unit(anchor_hit, anchor_candidate)
                if unit is None:
                    return self._select_failure(
                        request,
                        reason or REASON_REQUIRED_SOURCE_UNAVAILABLE,
                        "anchor_materialization",
                        envelope,
                    )
                required.append(unit)

        normalized_required, crossing = self._normalize_required(required)
        if crossing:
            return self._select_failure(
                request,
                REASON_OVERLAP_REQUIRES_EXPLICIT_REBUILD,
                "mandatory_crossing",
                envelope,
            )

        assembly = self._assemble(
            system=request.system_instructions,
            task=request.task_specification,
            units=normalized_required,
            rows=(),
            model=envelope.model,
            thinking=envelope.thinking,
            output_tokens=selected_output,
            tools=envelope.tools,
            replay=envelope.replay,
            response_format=envelope.response_format,
            counter_state=counter_state,
            revisions=request.editable_revisions,
        )
        if normalized_required:
            reason = self._fit_failure(
                assembly,
                units=normalized_required,
                cumulative=self._units_bytes(normalized_required),
            )
            if reason is not None:
                return self._select_failure(
                    request, reason, "required_fit", envelope, assembly
                )

        rows: list[dict[str, str]] = []
        map_revision: str | None = None
        if graph is not None:
            candidate_rows, map_revision = map_rows_from_graph(
                graph,
                scan,
                ranked_paths=ranked_paths,
                allowed_paths=scope,
                rows_max=self.context_config.map_rows_max,
                row_cache=self._row_cache,
                batch=batch,
            )
        else:
            candidate_rows = metadata_map_rows(
                catalog,
                ranked_paths=ranked_paths,
                rows_max=self.context_config.map_rows_max,
            )
            omitted.append(PackOmission(reason=REASON_MAP_UNAVAILABLE))
        for row in candidate_rows:
            if len(rows) >= self.context_config.map_rows_max:
                omitted.append(PackOmission(reason="map_limit", path=row["path"]))
                continue
            trial_rows = [*rows, row]
            trial = self._assemble(
                system=request.system_instructions,
                task=request.task_specification,
                units=normalized_required,
                rows=trial_rows,
                model=envelope.model,
                thinking=envelope.thinking,
                output_tokens=selected_output,
                tools=envelope.tools,
                replay=envelope.replay,
                response_format=envelope.response_format,
                counter_state=counter_state,
                revisions=request.editable_revisions,
            )
            if self._fits(trial, units=normalized_required, rows=trial_rows):
                rows = trial_rows
                assembly = trial
            else:
                omitted.append(PackOmission(reason="map_limit", path=row["path"]))

        if request.level == "l0":
            if not rows and not request.allow_empty_map:
                return self._select_failure(
                    request,
                    REASON_MAP_UNAVAILABLE,
                    "l0_map_empty",
                    envelope,
                    assembly,
                )
            result = self._ready(
                request,
                units=(),
                rows=rows,
                omitted=omitted,
                assembly=assembly,
                scan=scan,
                ranked=ranked,
                candidates=(),
                relations=(),
                map_revision=map_revision,
                deduplicated=0,
                considered=considered,
                envelope=envelope,
            )
            return result

        units = list(normalized_required)
        optional = self._optional_units(
            ranked, candidate_by_symbol, candidate_by_path, catalog, units
        )
        optional.sort(
            key=cmp_to_key(
                lambda a, b: self._compare_optional(a, b)
            )
        )
        for hit, unit in optional:
            if unit is None:
                continue
            if unit.digest != catalog.snapshot(unit.path).digest:  # type: ignore[union-attr]
                omitted.append(
                    PackOmission(reason=REASON_SOURCE_CHANGED, path=unit.path)
                )
                continue
            overlap = self._overlap_with_units(unit, units)
            if overlap is not None:
                if overlap in {"contains", "contained"}:
                    deduplicated += 1
                    omitted.append(PackOmission(reason="duplicate", path=unit.path))
                else:
                    omitted.append(PackOmission(reason="overlap", path=unit.path))
                continue
            if not self._char_caps_allow(units, unit):
                omitted.append(PackOmission(reason="oversize", path=unit.path))
                continue
            trial_units = [*units, unit]
            trial = self._assemble(
                system=request.system_instructions,
                task=request.task_specification,
                units=trial_units,
                rows=rows,
                model=envelope.model,
                thinking=envelope.thinking,
                output_tokens=selected_output,
                tools=envelope.tools,
                replay=envelope.replay,
                response_format=envelope.response_format,
                counter_state=counter_state,
                revisions=request.editable_revisions,
            )
            if self._fits(
                trial,
                units=trial_units,
                rows=rows,
                cumulative=self._units_bytes(trial_units),
            ):
                units = trial_units
                assembly = trial
            else:
                omitted.append(PackOmission(reason="budget", path=unit.path))

        fragments = self._fragments(units)
        revisions = self._revisions(fragments, rows, catalog)
        candidates = tuple(
            candidate_ref(candidate) for candidate in selected_candidates[:64]
        )
        relations = relation_evidence(graph, scan)
        result = self._ready(
            request,
            units=tuple(units),
            rows=rows,
            omitted=omitted,
            assembly=assembly,
            scan=scan,
            ranked=ranked,
            candidates=candidates,
            relations=relations,
            map_revision=map_revision,
            deduplicated=deduplicated,
            considered=considered,
            envelope=envelope,
            fragments=fragments,
            revisions=revisions,
        )
        return result

    def _ready(
        self,
        request: PackRequest,
        *,
        units: Sequence[SourceUnit],
        rows: Sequence[Mapping[str, str]],
        omitted: Sequence[PackOmission],
        assembly: _Assembly,
        scan: SourceScan,
        ranked: RetrievalResult,
        candidates: tuple[CandidateRef, ...],
        relations: tuple[Any, ...],
        map_revision: str | None,
        deduplicated: int,
        considered: int,
        envelope: "_EnvelopeView",
        fragments: tuple[PackedFragment, ...] | None = None,
        revisions: Mapping[str, str] | None = None,
    ) -> Composition:
        fragments = fragments if fragments is not None else self._fragments(units)
        revisions = (
            revisions
            if revisions is not None
            else self._revisions(fragments, rows, PackingCatalog(scan))
        )
        pack = PackedContextV2(
            level=request.level,
            task_id=request.task_id,
            status="ready",
            model=envelope.model,
            thinking=envelope.thinking,  # type: ignore[arg-type]
            tools=tuple(envelope.tools),
            response_format=dict(envelope.response_format)
            if envelope.response_format
            else None,
            fragments=fragments,
            map_rows=tuple(
                PackedMapRow(
                    row_id=_map_row_id(row["path"], row.get("digest", "")),
                    path=row["path"],
                    module=row["module"],
                    row=row["row"],
                    digest=row.get("digest") or None,
                    origin=row.get("origin", "metadata"),  # type: ignore[arg-type]
                )
                for row in rows
            ),
            omissions=tuple(omitted),
            reason_codes=tuple(ranked.degraded_reasons),
            included_fragment_ids=tuple(item.fragment_id for item in fragments),
            source_revisions=tuple(
                SourceRevision(path=path, digest=digest)
                for path, digest in sorted(revisions.items())
            ),
            candidate_refs=candidates,
            relation_evidence=tuple(relations),
            map_revision=map_revision,
            scan_incomplete=not scan.complete,
            degraded_reasons=tuple(ranked.degraded_reasons),
            messages=assembly.messages,
            metrics=self._metrics(
                assembly,
                considered=considered,
                selected=len(fragments) + len(rows),
                deduplicated=deduplicated,
                omitted=omitted,
                cumulative=self._units_bytes(units),
            ),
        )
        result = PackResult(status="ready", pack=pack)
        return Composition(
            result=result,
            revisions=revisions,
            fragments=fragments,
            candidates=candidates,
            relations=relations,
            policy=scan.policy,
            root_binding=_root_binding(self.repository),
            cumulative_units=self._units_bytes(units),
            model=envelope.model,
            thinking=envelope.thinking,
            output_tokens=envelope.output_tokens,
        )

    def _select_failure(
        self,
        request: PackRequest,
        reason: str,
        detail: str,
        envelope: "_EnvelopeView",
        assembly: _Assembly | None = None,
    ) -> Composition:
        measurement = None
        if assembly is not None:
            measurement = assembly.measurement
        else:
            counter_state = _CounterState(self.token_counter)
            measurement = self._assemble(
                system=request.system_instructions,
                task=request.task_specification,
                units=(),
                rows=(),
                model=envelope.model,
                thinking=envelope.thinking,
                output_tokens=envelope.output_tokens,
                tools=envelope.tools,
                replay=envelope.replay,
                response_format=envelope.response_format,
                counter_state=counter_state,
                revisions=request.editable_revisions,
            ).measurement
        result = self._failure(
            request,
            status="insufficient_context",
            reasons=(reason,),
            omissions=(PackOmission(reason=reason, detail=detail),),
            measurement=measurement,
            model=envelope.model,
        )
        return Composition(
            result=result,
            model=envelope.model,
            thinking=envelope.thinking,
            output_tokens=envelope.output_tokens,
        )

    # -- selection helpers -------------------------------------------
    @staticmethod
    def _required_anchor(
        ranked: RetrievalResult,
        candidate_by_symbol: Mapping[str, RankCandidate],
        candidate_by_path: Mapping[str, RankCandidate],
        catalog: PackingCatalog,
    ) -> tuple[RetrievalHit | None, RankCandidate | None]:
        for hit in ranked.hits:
            if not any(
                reason.signal in _DIRECT_SIGNALS and reason.weighted_points > 0
                for reason in hit.ranking_reasons
            ):
                continue
            snapshot = catalog.snapshot(hit.path)
            if snapshot is None or snapshot.digest != hit.source_digest:
                return None, None
            candidate = (
                candidate_by_symbol.get(hit.symbol_id or "")
                if hit.symbol_id
                else candidate_by_path.get(hit.path)
            )
            return hit, candidate
        return None, None

    def _normalize_required(
        self, units: Sequence[SourceUnit]
    ) -> tuple[list[SourceUnit], bool]:
        retained: list[SourceUnit] = []
        covered: list[SourceUnit] = []
        for unit in sorted(
            units, key=lambda item: (-(item.end_line - item.start_line), item.path, item.start_line)
        ):
            conflict = None
            for existing in retained:
                overlap = unit_overlap(unit, existing)
                if overlap == "contained":
                    covered.append(unit)
                    conflict = overlap
                    break
                if overlap == "contains":
                    # Keep the broader, earlier unit and fold the inner selectors.
                    conflict = overlap
                    break
                if overlap == "overlap":
                    return list(units), True
            if conflict is None:
                retained.append(unit)
        for inner in covered:
            for index, outer in enumerate(retained):
                if unit_overlap(inner, outer) in {"contained", "contains"}:
                    if inner.start_line >= outer.start_line and inner.end_line <= outer.end_line:
                        retained[index] = SourceUnit(
                            path=outer.path,
                            start_line=outer.start_line,
                            end_line=outer.end_line,
                            digest=outer.digest,
                            kind=outer.kind,
                            text=outer.text,
                            symbol=outer.symbol,
                            reasons=outer.reasons,
                            covered_selectors=tuple(
                                dict.fromkeys(
                                    (*outer.covered_selectors, *inner.covered_selectors)
                                )
                            ),
                        )
        retained.sort(key=lambda item: (item.path, item.start_line))
        return retained, False

    def _optional_units(
        self,
        ranked: RetrievalResult,
        candidate_by_symbol: Mapping[str, RankCandidate],
        candidate_by_path: Mapping[str, RankCandidate],
        catalog: PackingCatalog,
        selected: Sequence[SourceUnit],
    ) -> list[tuple[RetrievalHit, SourceUnit | None]]:
        result: list[tuple[RetrievalHit, SourceUnit | None]] = []
        for hit in ranked.hits:
            evidence = any(
                reason.signal in _EVIDENCE_SIGNALS and reason.weighted_points > 0
                for reason in hit.ranking_reasons
            )
            if not evidence:
                continue
            snapshot = catalog.snapshot(hit.path)
            if snapshot is None or snapshot.digest != hit.source_digest:
                continue
            candidate = (
                candidate_by_symbol.get(hit.symbol_id or "")
                if hit.symbol_id
                else candidate_by_path.get(hit.path)
            )
            unit, _reason = catalog.anchor_unit(hit, candidate)
            result.append((hit, unit))
        return result

    @staticmethod
    def _compare_optional(a: tuple[RetrievalHit, SourceUnit | None], b: tuple[RetrievalHit, SourceUnit | None]) -> int:
        hit_a, unit_a = a
        hit_b, unit_b = b
        cost_a = max(1, len(unit_a.text.encode("utf-8")) if unit_a else 1)
        cost_b = max(1, len(unit_b.text.encode("utf-8")) if unit_b else 1)
        left = hit_a.relevance_score * cost_b
        right = hit_b.relevance_score * cost_a
        if left != right:
            return -1 if left > right else 1
        key_a = (hit_a.path, hit_a.start_line or 0, hit_a.end_line or 0)
        key_b = (hit_b.path, hit_b.start_line or 0, hit_b.end_line or 0)
        if key_a == key_b:
            return 0
        return -1 if key_a < key_b else 1

    @staticmethod
    def _overlap_with_units(unit: SourceUnit, units: Sequence[SourceUnit]) -> str | None:
        for existing in units:
            overlap = unit_overlap(unit, existing)
            if overlap is not None:
                return overlap
        return None

    def _char_caps_allow(
        self, units: Sequence[SourceUnit], candidate: SourceUnit
    ) -> bool:
        per_file = sum(
            len(item.text)
            for item in units
            if item.path == candidate.path
        ) + len(candidate.text)
        if per_file > self.context_config.per_file_chars_max:
            return False
        total = sum(len(item.text) for item in units) + len(candidate.text)
        return total <= self.context_config.total_code_chars_max

    def _fits(
        self,
        assembly: _Assembly,
        *,
        units: Sequence[SourceUnit],
        rows: Sequence[Mapping[str, str]],
        cumulative: int | None = None,
    ) -> bool:
        measurement = assembly.measurement
        if self._section_failures(measurement):
            return False
        if measurement.total_tokens > self.packing_config.total_tokens:
            return False
        if cumulative is not None and cumulative > self.packing_config.cumulative_source_tokens:
            return False
        return self._char_caps_fit(units)

    def _fit_failure(
        self,
        assembly: _Assembly,
        *,
        units: Sequence[SourceUnit],
        cumulative: int,
    ) -> str | None:
        measurement = assembly.measurement
        failures = self._section_failures(measurement)
        if "source" in failures or "system" in failures or "task" in failures:
            return REASON_REQUIRED_SOURCE_OVER_BUDGET
        if measurement.total_tokens > self.packing_config.total_tokens:
            return REASON_TOTAL_BUDGET
        if cumulative > self.packing_config.cumulative_source_tokens:
            return REASON_CUMULATIVE_BUDGET
        if not self._char_caps_fit(units):
            return REASON_REQUIRED_SOURCE_OVER_BUDGET
        return None

    def _char_caps_fit(self, units: Sequence[SourceUnit]) -> bool:
        per_file: dict[str, int] = {}
        for unit in units:
            per_file[unit.path] = per_file.get(unit.path, 0) + len(unit.text)
        if any(value > self.context_config.per_file_chars_max for value in per_file.values()):
            return False
        return sum(per_file.values()) <= self.context_config.total_code_chars_max

    def _fragments(self, units: Sequence[SourceUnit]) -> tuple[PackedFragment, ...]:
        return tuple(
            PackedFragment(
                fragment_id=unit.fragment_id,
                path=unit.path,
                start_line=unit.start_line,
                end_line=unit.end_line,
                digest=unit.digest,
                kind=unit.kind,  # type: ignore[arg-type]
                text=unit.text,
                symbol=unit.symbol,
                reasons=unit.reasons,
                covered_selectors=unit.covered_selectors,
                prior_ids=unit.prior_ids,
            )
            for unit in units
        )

    def _revisions(
        self,
        fragments: Sequence[PackedFragment],
        rows: Sequence[Mapping[str, str]],
        catalog: PackingCatalog,
    ) -> dict[str, str]:
        revisions: dict[str, str] = {}
        for fragment in fragments:
            revisions[fragment.path] = fragment.digest
        for row in rows:
            digest = row.get("digest")
            if digest:
                revisions[row["path"]] = digest
        return revisions

    def _metrics(
        self,
        assembly: _Assembly,
        *,
        considered: int,
        selected: int,
        deduplicated: int,
        omitted: Sequence[PackOmission],
        cumulative: int,
    ) -> PackMetrics:
        reasons: dict[str, int] = {}
        for item in omitted:
            reasons[item.reason] = reasons.get(item.reason, 0) + 1
        measurement = assembly.measurement
        return PackMetrics(
            estimator=measurement.estimator,  # type: ignore[arg-type]
            considered=considered,
            selected=selected,
            deduplicated=deduplicated,
            omitted_by_reason=reasons,
            section_tokens=dict(measurement.section_tokens),
            section_limits=self._section_limits(),
            input_tokens=measurement.input_tokens,
            output_tokens=measurement.output_tokens,
            total_tokens=measurement.total_tokens,
            remaining_total=measurement.remaining_total,
            cumulative_source_units=cumulative,
        )

    def _failure(
        self,
        request: PackRequest,
        *,
        status: PackStatus,
        reasons: Sequence[str],
        omissions: Sequence[PackOmission],
        measurement: TokenMeasurement,
        model: str,
    ) -> PackResult:
        pack = PackedContextV2(
            level=request.level,
            task_id=request.task_id,
            status=status,
            model=model,
            omissions=tuple(omissions),
            reason_codes=tuple(reasons),
            messages=(),
            metrics=PackMetrics(
                estimator=measurement.estimator,  # type: ignore[arg-type]
                section_tokens=dict(measurement.section_tokens),
                section_limits=self._section_limits(),
                input_tokens=measurement.input_tokens,
                output_tokens=measurement.output_tokens,
                total_tokens=measurement.total_tokens,
                remaining_total=measurement.remaining_total,
                omitted_by_reason={
                    item.reason: sum(1 for other in omissions if other.reason == item.reason)
                    for item in omissions
                },
            ),
        )
        return PackResult(status=status, pack=pack, reason_codes=tuple(reasons))

    # -- bounded L2 expansion ---------------------------------------
    def expand(
        self,
        session: PackingSession,
        request: ExpansionRequest,
        *,
        envelope: PackEnvelope | None = None,
        scan_paths: frozenset[str] | None = None,
        read_filter=None,
    ) -> PackResult:
        if not isinstance(session, PackingSession) or not session.open:
            raise InvalidContextRequest("A live packing session is required")
        if not isinstance(request, ExpansionRequest):
            raise InvalidContextRequest("An explicit expansion request is required")
        if session.attempts >= session.max_expansions:
            return self._expand_failure(
                session, REASON_EXPANSION_LIMIT, "attempt_limit", None
            )

        envelope_view = self._session_envelope(session, envelope)
        scope = self._session_scope(session)
        edge = None
        if request.kind == "dependency":
            edge = self._registered_edge(session, request.edge_id or "")
            if scope is not None and edge.related_path not in scope:
                raise InvalidContextRequest("Expansion target is outside the output scope")
        else:
            self._check_registered_digest(
                session, request.path or "", request.expected_digest or ""
            )
            if scope is not None and (request.path or "") not in scope:
                raise InvalidContextRequest("Expansion target is outside the output scope")

        session.attempts += 1
        counter_state = _CounterState(self.token_counter)
        batch = self._cache.begin_batch() if self._cache is not None else None
        try:
            with self.repository.scan_retrieval_sources(
                paths=scan_paths, read_filter=read_filter
            ) as scan:
                catalog = PackingCatalog(
                    scan,
                    allowed_paths=scope,
                    fact_cache=self._fact_cache,
                    span_cache=self._span_cache,
                    batch=batch,
                )
                drift = self._session_drift(session, scan, catalog)
                if drift is not None:
                    if drift == REASON_SOURCE_SCAN_INCOMPLETE:
                        return self._expand_failure(
                            session, drift, "recorded_path_unverifiable", None
                        )
                    session.close()
                    return self._stale_result(session, envelope_view)

                resolved = self._resolve_expansion(
                    session, request, scan, catalog, edge
                )
                if isinstance(resolved, PackResult):
                    return resolved

                target = resolved
                coverage = session.merged_coverage(target.path, target.digest)
                delta_units, carry_ids, status = self._delta_for_target(
                    session, catalog, target, coverage, request
                )
                if status is not None:
                    if status == "no_change":
                        return self._no_change(session, target, envelope_view)
                    if status == REASON_SOURCE_CHANGED:
                        session.close()
                        return self._stale_result(session, envelope_view)
                    return self._expand_failure(session, status, "target_overlap", None)

                new_bytes = self._units_bytes(delta_units)
                file_chars = dict(session.file_chars)
                for unit in delta_units:
                    file_chars[unit.path] = file_chars.get(unit.path, 0) + len(unit.text)
                if any(
                    value > self.context_config.per_file_chars_max
                    for value in file_chars.values()
                ):
                    return self._expand_failure(
                        session, REASON_REQUIRED_SOURCE_OVER_BUDGET, "per_file_chars", None
                    )
                if (
                    sum(file_chars.values()) > self.context_config.total_code_chars_max
                ):
                    return self._expand_failure(
                        session, REASON_REQUIRED_SOURCE_OVER_BUDGET, "total_code_chars", None
                    )
                assembly = self._assemble(
                    system=session.task.system_instructions,
                    task=session.task.task_specification,
                    units=delta_units,
                    rows=(),
                    model=session.model,
                    thinking=session.thinking,
                    output_tokens=session.output_tokens,
                    tools=envelope_view.tools,
                    replay=envelope_view.replay,
                    response_format=envelope_view.response_format,
                    counter_state=counter_state,
                )
                if not self._fits(
                    assembly,
                    units=delta_units,
                    rows=(),
                    cumulative=session.cumulative_units + new_bytes,
                ):
                    return self._expand_failure(
                        session,
                        self._fit_failure(
                            assembly,
                            units=delta_units,
                            cumulative=session.cumulative_units + new_bytes,
                        )
                        or REASON_REQUIRED_SOURCE_OVER_BUDGET,
                        "expansion_fit",
                        assembly,
                    )
                if not scan.revalidate():
                    if batch is not None:
                        batch.discard()
                    session.close()
                    return self._stale_result(session, envelope_view)
                if batch is not None and self._cache is not None:
                    self._cache.flush(batch)

                fragments = tuple(
                    PackedFragment(
                        fragment_id=unit.fragment_id,
                        path=unit.path,
                        start_line=unit.start_line,
                        end_line=unit.end_line,
                        digest=unit.digest,
                        kind="delta",
                        text=unit.text,
                        symbol=unit.symbol,
                        reasons=unit.reasons,
                        covered_selectors=unit.covered_selectors,
                        prior_ids=carry_ids,
                    )
                    for unit in delta_units
                )
                receipt = ExpansionDelivery(
                    receipt_id="pack-receipt:" + uuid.uuid4().hex,
                    target_kind=request.kind,
                    delta_ids=tuple(item.fragment_id for item in fragments),
                    carry_forward_ids=carry_ids,
                    envelope_fingerprint=self._envelope_fingerprint(envelope_view),
                    source_refs=tuple(
                        FragmentRef(
                            fragment_id=item.fragment_id,
                            path=item.path,
                            start_line=item.start_line,
                            end_line=item.end_line,
                            digest=item.digest,
                            kind=item.kind,
                        )
                        for item in fragments
                    ),
                    edge_id=request.edge_id,
                )
                for unit in delta_units:
                    session.add_coverage(
                        unit.path, unit.digest, unit.start_line, unit.end_line
                    )
                    session.record_fragment(
                        FragmentRef(
                            fragment_id=unit.fragment_id,
                            path=unit.path,
                            start_line=unit.start_line,
                            end_line=unit.end_line,
                            digest=unit.digest,
                            kind="delta",
                        )
                    )
                session.record_receipt(receipt)
                session.cumulative_units += new_bytes
                session.file_chars = file_chars
                session.total_chars = sum(file_chars.values())
                pack = PackedContextV2(
                    level=session.task.level,  # type: ignore[arg-type]
                    task_id=session.task.task_id,
                    status="ready",
                    model=session.model,
                    thinking=session.thinking,  # type: ignore[arg-type]
                    tools=tuple(envelope_view.tools),
                    response_format=dict(envelope_view.response_format)
                    if envelope_view.response_format
                    else None,
                    fragments=fragments,
                    omissions=(),
                    reason_codes=(),
                    included_fragment_ids=tuple(
                        item.fragment_id for item in fragments
                    ),
                    prior_fragment_ids=carry_ids,
                    source_revisions=tuple(
                        SourceRevision(path=unit.path, digest=unit.digest)
                        for unit in delta_units
                    ),
                    scan_incomplete=not scan.complete,
                    messages=assembly.messages,
                    metrics=self._metrics(
                        assembly,
                        considered=1,
                        selected=len(fragments),
                        deduplicated=0,
                        omitted=(),
                        cumulative=session.cumulative_units,
                    ),
                )
                log.debug(
                    "[Context.packer] expansion kind=%s units=%s attempts=%s cumulative=%s",
                    request.kind,
                    len(fragments),
                    session.attempts,
                    session.cumulative_units,
                )
                return PackResult(
                    status="ready", pack=pack, receipt_id=receipt.receipt_id
                )
        except ContextScanError:
            return self._expand_failure(
                session, REASON_SOURCE_UNAVAILABLE, "scan_unavailable", None
            )

    def prepare_expansion_delivery(
        self,
        session: PackingSession,
        receipt_id: str,
        carry_forward_ids: Sequence[str],
        *,
        envelope: PackEnvelope | None = None,
    ) -> PackResult:
        if not isinstance(session, PackingSession) or not session.open:
            raise InvalidContextRequest("A live packing session is required")
        receipt = session.receipts.get(receipt_id)
        if receipt is None:
            raise InvalidContextRequest("Unknown expansion delivery receipt")
        provided = tuple(carry_forward_ids)
        if len(provided) != len(set(provided)):
            raise InvalidContextRequest("Carry-forward IDs must be unique")
        if set(provided) != set(receipt.carry_forward_ids) or len(provided) != len(
            receipt.carry_forward_ids
        ):
            raise InvalidContextRequest("Carry-forward IDs do not match the receipt")

        envelope_view = self._session_envelope(session, envelope)
        if self._envelope_fingerprint(envelope_view) != receipt.envelope_fingerprint:
            raise InvalidContextRequest("Expansion envelope changed before delivery")

        scope = self._session_scope(session)
        counter_state = _CounterState(self.token_counter)
        try:
            with self.repository.scan_retrieval_sources() as scan:
                catalog = PackingCatalog(scan, allowed_paths=scope)
                drift = self._session_drift(session, scan, catalog)
                if drift is not None:
                    if drift == REASON_SOURCE_SCAN_INCOMPLETE:
                        return self._expand_failure(
                            session, drift, "delivery_unverifiable", None
                        )
                    session.close()
                    return self._stale_result(session, envelope_view)
                if receipt.edge_id is not None:
                    if not self._revalidate_edge(session, receipt.edge_id, scan):
                        session.close()
                        return self._stale_result(session, envelope_view)
                delta_units = self._rematerialize(catalog, receipt.source_refs, "delta")
                carry_units = self._rematerialize_ids(
                    catalog, session, receipt.carry_forward_ids
                )
                if delta_units is None or carry_units is None:
                    session.close()
                    return self._stale_result(session, envelope_view)
                units = [*carry_units, *delta_units]
                assembly = self._assemble(
                    system=session.task.system_instructions,
                    task=session.task.task_specification,
                    units=units,
                    rows=(),
                    model=session.model,
                    thinking=session.thinking,
                    output_tokens=session.output_tokens,
                    tools=envelope_view.tools,
                    replay=envelope_view.replay,
                    response_format=envelope_view.response_format,
                    counter_state=counter_state,
                )
                if not self._fits(
                    assembly,
                    units=units,
                    rows=(),
                    cumulative=session.cumulative_units,
                ):
                    return self._expand_failure(
                        session,
                        REASON_REQUIRED_SOURCE_OVER_BUDGET,
                        "delivery_fit",
                        assembly,
                    )
                if not scan.revalidate():
                    session.close()
                    return self._stale_result(session, envelope_view)
                fragments = tuple(
                    PackedFragment(
                        fragment_id=unit.fragment_id,
                        path=unit.path,
                        start_line=unit.start_line,
                        end_line=unit.end_line,
                        digest=unit.digest,
                        kind="delta",
                        text=unit.text,
                        symbol=unit.symbol,
                        reasons=unit.reasons,
                        covered_selectors=unit.covered_selectors,
                        prior_ids=receipt.carry_forward_ids if unit in delta_units else (),
                    )
                    for unit in units
                )
                pack = PackedContextV2(
                    level=session.task.level,  # type: ignore[arg-type]
                    task_id=session.task.task_id,
                    status="ready",
                    model=session.model,
                    thinking=session.thinking,  # type: ignore[arg-type]
                    tools=tuple(envelope_view.tools),
                    response_format=dict(envelope_view.response_format)
                    if envelope_view.response_format
                    else None,
                    fragments=fragments,
                    included_fragment_ids=tuple(
                        item.fragment_id for item in delta_units
                    ),
                    prior_fragment_ids=receipt.carry_forward_ids,
                    source_revisions=tuple(
                        SourceRevision(path=unit.path, digest=unit.digest)
                        for unit in units
                    ),
                    scan_incomplete=not scan.complete,
                    messages=assembly.messages,
                    metrics=self._metrics(
                        assembly,
                        considered=len(delta_units) + len(carry_units),
                        selected=len(fragments),
                        deduplicated=0,
                        omitted=(),
                        cumulative=session.cumulative_units,
                    ),
                )
                return PackResult(
                    status="ready", pack=pack, receipt_id=receipt.receipt_id
                )
        except ContextScanError:
            return self._expand_failure(
                session, REASON_SOURCE_UNAVAILABLE, "delivery_scan_unavailable", None
            )

    # -- expansion helpers ------------------------------------------
    @staticmethod
    def _session_scope(session: PackingSession) -> frozenset[str] | None:
        return (
            frozenset(session.task.output_paths)
            if session.task.output_paths is not None
            else None
        )

    def _session_envelope(
        self, session: PackingSession, envelope: PackEnvelope | None
    ) -> "_EnvelopeView":
        selected = envelope or PackEnvelope()
        self._validate_replay(selected.replay, session.thinking)
        normalize_chat_request(
            model=session.model,
            messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "task"},
            ],
            tools=selected.tools or None,
            response_format=selected.response_format,
            thinking=session.thinking,  # type: ignore[arg-type]
            output_cap=session.output_tokens,
            max_output_tokens=None,
        )
        return self._EnvelopeView(
            model=session.model,
            thinking=session.thinking,
            tools=selected.tools,
            replay=selected.replay,
            response_format=selected.response_format,
        )

    @staticmethod
    def _envelope_fingerprint(envelope: "_EnvelopeView") -> str:
        payload = {
            "model": envelope.model,
            "thinking": envelope.thinking,
            "tools": list(envelope.tools),
            "replay": list(envelope.replay),
            "response_format": envelope.response_format,
            "output_tokens": None,
        }
        return wire_fingerprint(payload)

    def _registered_digest(self, session: PackingSession, path: str) -> str | None:
        if path in session.revisions:
            return session.revisions[path]
        for candidate in session.candidates:
            if candidate.path == path:
                return candidate.digest
        return None

    def _check_registered_digest(
        self, session: PackingSession, path: str, expected: str
    ) -> None:
        registered = self._registered_digest(session, path)
        if registered is None:
            raise InvalidContextRequest("Expansion target is not a registered reference")
        if registered != expected:
            raise InvalidContextRequest("Expansion digest does not match the registered reference")

    @staticmethod
    def _registered_edge(session: PackingSession, edge_id: str):
        for item in session.relations:
            if item.edge_id == edge_id:
                return item
        raise InvalidContextRequest("Expansion edge is not a registered reference")

    def _session_drift(
        self,
        session: PackingSession,
        scan: SourceScan,
        catalog: PackingCatalog,
    ) -> str | None:
        if _root_binding(self.repository) != session.root_binding:
            return REASON_SOURCE_CHANGED
        if scan.policy != session.policy:
            return REASON_SOURCE_CHANGED
        for path, digest in session.revisions.items():
            snapshot = catalog.snapshot(path)
            if snapshot is None:
                if scan.complete:
                    return REASON_SOURCE_CHANGED
                return REASON_SOURCE_SCAN_INCOMPLETE
            if snapshot.digest != digest:
                return REASON_SOURCE_CHANGED
        return None

    def _resolve_expansion(
        self,
        session: PackingSession,
        request: ExpansionRequest,
        scan: SourceScan,
        catalog: PackingCatalog,
        edge: Any,
    ) -> SourceUnit | PackResult:
        if request.kind == "symbol":
            assert request.path is not None
            snapshot = catalog.snapshot(request.path)
            if snapshot is None:
                return self._expand_failure(
                    session,
                    REASON_REQUIRED_SOURCE_UNAVAILABLE if not scan.complete else REASON_SOURCE_CHANGED,
                    "symbol_path_unavailable",
                    None,
                )
            if snapshot.digest != request.expected_digest:
                session.close()
                return self._stale_result(session, self._session_envelope(session, None))
            unit, reason = catalog.symbol_unit(
                request.path,
                request.qualified_name or "",
                occurrence=request.occurrence,
                reasons=("explicit_expansion", "symbol"),
            )
            if unit is None:
                return self._expand_failure(
                    session,
                    reason or REASON_REQUIRED_SOURCE_UNAVAILABLE,
                    "symbol_resolution",
                    None,
                )
            return unit
        if request.kind == "range":
            assert request.path is not None
            snapshot = catalog.snapshot(request.path)
            if snapshot is None:
                return self._expand_failure(
                    session,
                    REASON_REQUIRED_SOURCE_UNAVAILABLE if not scan.complete else REASON_SOURCE_CHANGED,
                    "range_path_unavailable",
                    None,
                )
            if snapshot.digest != request.expected_digest:
                session.close()
                return self._stale_result(session, self._session_envelope(session, None))
            if request.full_file:
                line_count = len(iter_lines(snapshot.text))
                if (request.start_line, request.end_line) != (1, max(1, line_count)):
                    raise InvalidContextRequest(
                        "Full-file expansion must match the admitted file extent"
                    )
            unit, reason = catalog.range_unit(
                request.path,
                request.start_line or 1,
                request.end_line or 1,
                full_file=request.full_file,
                reasons=("explicit_expansion", "full_file" if request.full_file else "range"),
            )
            if unit is None:
                return self._expand_failure(
                    session,
                    reason or REASON_INVALID_STRUCTURAL_RANGE,
                    "range_resolution",
                    None,
                )
            return unit
        assert edge is not None
        snapshot = catalog.snapshot(edge.related_path)
        importer = catalog.snapshot(edge.importer_path)
        if snapshot is None or importer is None:
            return self._expand_failure(
                session,
                REASON_DEPENDENCY_UNAVAILABLE,
                "dependency_endpoint_unavailable",
                None,
            )
        if (
            snapshot.digest != edge.related_digest
            or importer.digest != edge.importer_digest
        ):
            session.close()
            return self._stale_result(session, self._session_envelope(session, None))
        if self.retriever.load_graph is None or not scan.complete:
            return self._expand_failure(
                session, REASON_DEPENDENCY_UNAVAILABLE, "graph_unavailable", None
            )
        if self.retriever.load_graph is not None and scan.complete:
            try:
                graph = self.retriever.load_graph(scan)
            except CodeIndexError:
                return self._expand_failure(
                    session, REASON_DEPENDENCY_UNAVAILABLE, "graph_unavailable", None
                )
            except Exception:
                raise
            if edge.edge_id not in {
                item.edge_id for item in relation_evidence(graph, scan)
            }:
                return self._expand_failure(
                    session, REASON_DEPENDENCY_UNAVAILABLE, "edge_unresolved", None
                )
        unit = catalog.first_definition_for_path(edge.related_path, ())
        if unit is None:
            return self._expand_failure(
                session, REASON_DEPENDENCY_UNAVAILABLE, "related_unit_unavailable", None
            )
        return unit

    def _delta_for_target(
        self,
        session: PackingSession,
        catalog: PackingCatalog,
        target: SourceUnit,
        coverage: tuple[tuple[int, int], ...],
        request: ExpansionRequest,
    ) -> tuple[list[SourceUnit], tuple[str, ...], str | None]:
        target_interval = (target.start_line, target.end_line)
        if self._interval_covered(coverage, target_interval):
            return [], (), "no_change"
        carry_ids = tuple(
            fragment_id
            for fragment_id, ref in session.fragments.items()
            if ref.path == target.path
            and ref.digest == target.digest
            and not (ref.end_line < target.start_line or ref.start_line > target.end_line)
        )
        if not coverage:
            return [target], carry_ids, None
        remaining = self._subtract(target_interval, coverage)
        if not remaining:
            return [], (), "no_change"
        delta: list[SourceUnit] = []
        for start, end in remaining:
            if is_python(target.path):
                unit, reason = catalog.range_unit(
                    target.path,
                    start,
                    end,
                    reasons=("explicit_expansion", "overlap_delta"),
                )
                if unit is None or unit.start_line != start or unit.end_line != end:
                    return [], (), REASON_OVERLAP_REQUIRES_EXPLICIT_REBUILD
            else:
                unit, reason = catalog.range_unit(
                    target.path,
                    start,
                    end,
                    reasons=("explicit_expansion", "overlap_delta"),
                )
                if unit is None:
                    return [], (), REASON_OVERLAP_REQUIRES_EXPLICIT_REBUILD
            delta.append(unit)
        union: tuple[tuple[int, int], ...] = coverage
        for unit in delta:
            union = _merge_intervals((*union, (unit.start_line, unit.end_line)))
        if not self._interval_covered(union, target_interval):
            return [], (), REASON_OVERLAP_REQUIRES_EXPLICIT_REBUILD
        return delta, carry_ids, None

    @staticmethod
    def _interval_covered(
        intervals: Sequence[tuple[int, int]], target: tuple[int, int]
    ) -> bool:
        start, end = target
        return any(a <= start and b >= end for a, b in intervals)

    @staticmethod
    def _subtract(
        target: tuple[int, int], coverage: Sequence[tuple[int, int]]
    ) -> list[tuple[int, int]]:
        remaining = [target]
        for cov_start, cov_end in coverage:
            next_remaining: list[tuple[int, int]] = []
            for start, end in remaining:
                if cov_end < start or cov_start > end:
                    next_remaining.append((start, end))
                    continue
                if cov_start > start:
                    next_remaining.append((start, cov_start - 1))
                if cov_end < end:
                    next_remaining.append((cov_end + 1, end))
            remaining = next_remaining
        return remaining

    def _rematerialize(
        self,
        catalog: PackingCatalog,
        refs: Sequence[FragmentRef],
        kind: str,
    ) -> list[SourceUnit] | None:
        units: list[SourceUnit] = []
        for ref in refs:
            snapshot = catalog.snapshot(ref.path)
            if snapshot is None or snapshot.digest != ref.digest:
                return None
            line_count = len(iter_lines(snapshot.text))
            if line_count == 0:
                if (ref.start_line, ref.end_line) != (1, 1):
                    return None
                text = ""
            else:
                if ref.start_line < 1 or ref.end_line > line_count:
                    return None
                text = slice_lines(snapshot.text, ref.start_line, ref.end_line)
            units.append(
                SourceUnit(
                    path=ref.path,
                    start_line=ref.start_line,
                    end_line=ref.end_line,
                    digest=ref.digest,
                    kind=kind,
                    text=text,
                    reasons=(kind,),
                )
            )
        return units

    def _rematerialize_ids(
        self,
        catalog: PackingCatalog,
        session: PackingSession,
        ids: Sequence[str],
    ) -> list[SourceUnit] | None:
        refs: list[FragmentRef] = []
        for fragment_id in ids:
            ref = session.fragments.get(fragment_id)
            if ref is None:
                return None
            refs.append(ref)
        return self._rematerialize(catalog, refs, "carry_forward")

    def _revalidate_edge(
        self, session: PackingSession, edge_id: str, scan: SourceScan
    ) -> bool:
        edge = self._registered_edge(session, edge_id)
        digests = {item.path: item.digest for item in scan.snapshots}
        return digests.get(edge.related_path) == edge.related_digest

    def _expand_failure(
        self,
        session: PackingSession,
        reason: str,
        detail: str,
        assembly: _Assembly | None,
    ) -> PackResult:
        measurement = assembly.measurement if assembly is not None else None
        if measurement is None:
            counter_state = _CounterState(self.token_counter)
            measurement = self._assemble(
                system=session.task.system_instructions,
                task=session.task.task_specification,
                units=(),
                rows=(),
                model=session.model,
                thinking=session.thinking,
                output_tokens=session.output_tokens,
                tools=(),
                replay=(),
                response_format=None,
                counter_state=counter_state,
            ).measurement
        pack = PackedContextV2(
            level=session.task.level,  # type: ignore[arg-type]
            task_id=session.task.task_id,
            status="insufficient_context",
            model=session.model,
            omissions=(PackOmission(reason=reason, detail=detail),),
            reason_codes=(reason,),
            messages=(),
            metrics=self._metrics(
                _Assembly(
                    sections={},
                    messages=(),
                    wire={},
                    measurement=measurement,
                ),
                considered=0,
                selected=0,
                deduplicated=0,
                omitted=(PackOmission(reason=reason, detail=detail),),
                cumulative=session.cumulative_units,
            ),
        )
        return PackResult(status="insufficient_context", pack=pack, reason_codes=(reason,))

    def _stale_result(
        self, session: PackingSession, envelope: "_EnvelopeView"
    ) -> PackResult:
        counter_state = _CounterState(self.token_counter)
        measurement = self._assemble(
            system=session.task.system_instructions,
            task=session.task.task_specification,
            units=(),
            rows=(),
            model=session.model,
            thinking=session.thinking,
            output_tokens=session.output_tokens,
            tools=envelope.tools,
            replay=envelope.replay,
            response_format=envelope.response_format,
            counter_state=counter_state,
        ).measurement
        pack = PackedContextV2(
            level=session.task.level,  # type: ignore[arg-type]
            task_id=session.task.task_id,
            status="stale_reference",
            model=session.model,
            omissions=(PackOmission(reason=REASON_SOURCE_CHANGED),),
            reason_codes=(REASON_SOURCE_CHANGED,),
            messages=(),
            metrics=PackMetrics(
                estimator=measurement.estimator,  # type: ignore[arg-type]
                section_tokens=dict(measurement.section_tokens),
                section_limits=self._section_limits(),
                input_tokens=measurement.input_tokens,
                output_tokens=measurement.output_tokens,
                total_tokens=measurement.total_tokens,
                remaining_total=measurement.remaining_total,
                cumulative_source_units=session.cumulative_units,
                attempts=session.attempts,
                remaining_expansions=session.remaining_expansions,
            ),
        )
        return PackResult(
            status="stale_reference", pack=pack, reason_codes=(REASON_SOURCE_CHANGED,)
        )

    def _no_change(
        self,
        session: PackingSession,
        target: SourceUnit,
        envelope: "_EnvelopeView",
    ) -> PackResult:
        counter_state = _CounterState(self.token_counter)
        measurement = self._assemble(
            system=session.task.system_instructions,
            task=session.task.task_specification,
            units=(),
            rows=(),
            model=session.model,
            thinking=session.thinking,
            output_tokens=session.output_tokens,
            tools=envelope.tools,
            replay=envelope.replay,
            response_format=envelope.response_format,
            counter_state=counter_state,
        ).measurement
        pack = PackedContextV2(
            level=session.task.level,  # type: ignore[arg-type]
            task_id=session.task.task_id,
            status="no_change",
            model=session.model,
            source_revisions=(
                SourceRevision(path=target.path, digest=target.digest),
            ),
            messages=(),
            metrics=PackMetrics(
                estimator=measurement.estimator,  # type: ignore[arg-type]
                section_tokens=dict(measurement.section_tokens),
                section_limits=self._section_limits(),
                input_tokens=measurement.input_tokens,
                output_tokens=measurement.output_tokens,
                total_tokens=measurement.total_tokens,
                remaining_total=measurement.remaining_total,
                cumulative_source_units=session.cumulative_units,
                attempts=session.attempts,
                remaining_expansions=session.remaining_expansions,
            ),
        )
        return PackResult(status="no_change", pack=pack)

    @staticmethod
    def _units_bytes(units: Sequence[SourceUnit]) -> int:
        return sum(len(unit.text.encode("utf-8")) for unit in units)


def _map_row_id(path: str, digest: str) -> str:
    payload = json.dumps(["map", path, digest], separators=(",", ":"), ensure_ascii=False)
    return "map:sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _root_binding(repository: Repository) -> tuple[str, int, int] | None:
    try:
        info = os.stat(repository.root, follow_symlinks=False)
    except OSError:
        return None
    return (str(repository.root), info.st_dev, info.st_ino)


def _merge_intervals(
    intervals: Sequence[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return tuple(merged)
