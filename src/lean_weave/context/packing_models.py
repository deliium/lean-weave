"""Versioned, immutable contracts for token-aware context packing.

The V2 contracts are additive: :class:`ContextPack`, ``ContextQuery`` and the
stage-scoped ``ContextProvider.build`` interface are untouched.  A
:class:`PackedContextV2` is only dispatchable while its status is ``ready``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lean_weave.context.models import (
    normalize_relative_path,
    validate_text,
)
from lean_weave.context.retrieval_models import validate_retrieval_query
from lean_weave.errors import InvalidContextRequest

PackLevel = Literal["l0", "l1"]
PackStatus = Literal["ready", "insufficient_context", "stale_reference", "no_change"]
FragmentKind = Literal["definition", "range", "full", "delta", "map"]

REASON_NO_RELEVANT_SOURCE = "no_relevant_source"
REASON_REQUIRED_SOURCE_UNAVAILABLE = "required_source_unavailable"
REASON_REQUIRED_SOURCE_OVER_BUDGET = "required_source_over_budget"
REASON_MAP_UNAVAILABLE = "map_unavailable"
REASON_OVERLAP_REQUIRES_EXPLICIT_REBUILD = "overlap_requires_explicit_rebuild"
REASON_INVALID_STRUCTURAL_RANGE = "invalid_structural_range"
REASON_DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
REASON_SOURCE_SCAN_INCOMPLETE = "source_scan_incomplete"
REASON_SOURCE_UNAVAILABLE = "source_unavailable"
REASON_SOURCE_CHANGED = "source_changed"
REASON_SECTION_BUDGET = "section_budget"
REASON_TOTAL_BUDGET = "total_budget"
REASON_CUMULATIVE_BUDGET = "cumulative_budget"
REASON_EXPANSION_LIMIT = "expansion_limit"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _copy_json(value: Any) -> Any:
    """Deep-copy JSON-shaped data so callers cannot mutate a request later."""

    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError, RecursionError):
        raise ValueError("envelope content must be JSON-compatible") from None


def _normalize_paths(values: tuple[str, ...] | list[str] | None) -> tuple[str, ...] | None:
    if values is None:
        return None
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = normalize_relative_path(value)
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return tuple(result)


def validate_task_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("task_id must be a string")
    stripped = value.strip()
    if not stripped or len(stripped) > 128:
        raise ValueError("task_id must be a non-empty identifier of at most 128 characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in stripped):
        raise ValueError("task_id must not contain control characters")
    if stripped != value:
        raise ValueError("task_id must not contain surrounding whitespace")
    return stripped


class SymbolSelector(_Model):
    """Exact required definition reference (path + qualified name)."""

    path: str
    qualified_name: str
    occurrence: int | None = Field(default=None, ge=0)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return normalize_relative_path(value)

    @field_validator("qualified_name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("qualified_name must be non-empty")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("qualified_name must not contain control characters")
        return value


class PackEnvelope(_Model):
    """Typed per-call envelope (never implicit history or credentials)."""

    model: str | None = None
    thinking: Literal["enabled", "disabled"] | None = None
    tools: tuple[dict[str, Any], ...] = ()
    replay: tuple[dict[str, Any], ...] = ()
    response_format: dict[str, Any] | None = None

    @field_validator("tools", "replay", mode="before")
    @classmethod
    def copy_sequence(cls, value: Any) -> Any:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("envelope sequence must be a list or tuple")
        return tuple(_copy_json(item) for item in value)

    @field_validator("response_format", mode="before")
    @classmethod
    def copy_format(cls, value: Any) -> Any:
        if value is None:
            return None
        return _copy_json(value)


class PackRequest(_Model):
    """Explicit whole-checkout packing preview request.

    ``output_paths`` narrows what may be emitted; it does not narrow the
    whole-checkout ranking read and is not a workflow read approval.  The
    workflow-only fields below are internal, default-preserving extensions.
    """

    task_id: str
    query: str
    task_specification: str
    system_instructions: str
    level: PackLevel = "l1"
    required_symbols: tuple[SymbolSelector, ...] = ()
    output_paths: tuple[str, ...] | None = None
    envelope: PackEnvelope = Field(default_factory=PackEnvelope)
    # Private workflow extensions: no public preview behavior changes.
    required_paths: tuple[str, ...] = ()
    forced_fragments: tuple[FragmentRef, ...] = ()
    editable_revisions: tuple[tuple[str, str], ...] = ()
    allow_empty_map: bool = False
    output_tokens: int | None = Field(default=None, gt=0)

    @field_validator("task_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return validate_task_id(value)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        return validate_retrieval_query(value)

    @field_validator("task_specification")
    @classmethod
    def validate_task(cls, value: str) -> str:
        return validate_text(value, what="task specification")

    @field_validator("system_instructions")
    @classmethod
    def validate_system(cls, value: str) -> str:
        return validate_text(value, what="system instructions")

    @field_validator("output_paths", mode="before")
    @classmethod
    def normalize_scope(cls, value: Any) -> Any:
        return _normalize_paths(value)

    @field_validator("required_paths", mode="before")
    @classmethod
    def normalize_required(cls, value: Any) -> Any:
        return _normalize_paths(value) or ()

    @field_validator("editable_revisions", mode="before")
    @classmethod
    def normalize_revisions(cls, value: Any) -> Any:
        if value is None:
            return ()
        result: list[tuple[str, str]] = []
        seen: set[str] = set()
        for path, revision in value:
            normalized = normalize_relative_path(path)
            if normalized in seen:
                continue
            if not isinstance(revision, str) or not revision:
                raise ValueError("editable revision must be non-empty")
            seen.add(normalized)
            result.append((normalized, revision))
        return tuple(result)

    @model_validator(mode="after")
    def validate_level_requirements(self) -> "PackRequest":
        if self.level == "l0" and self.required_symbols:
            raise ValueError("L0 packs cannot require source definitions")
        if self.level == "l0" and (self.required_paths or self.forced_fragments):
            raise ValueError("L0 packs cannot require source paths")
        if self.forced_fragments and self.level != "l1":
            raise ValueError("Forced fragments require an L1 pack")
        return self


class FragmentRef(_Model):
    """Exact, revision-bearing reference to one emitted source unit."""

    fragment_id: str
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    digest: str
    kind: FragmentKind

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return normalize_relative_path(value)

    @model_validator(mode="after")
    def validate_bounds(self) -> "FragmentRef":
        if self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line")
        return self


class PackedFragment(FragmentRef):
    """A fragment reference plus canonical source and selection reasons."""

    text: str
    symbol: str | None = None
    reasons: tuple[str, ...] = ()
    covered_selectors: tuple[str, ...] = ()
    prior_ids: tuple[str, ...] = ()


class SourceRevision(_Model):
    path: str
    digest: str

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return normalize_relative_path(value)


class PackedMapRow(_Model):
    row_id: str
    path: str
    module: str
    row: str
    digest: str | None = None
    origin: Literal["ranked", "lexical", "metadata", "indexed"] = "metadata"

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return normalize_relative_path(value)


class CandidateRef(_Model):
    """Bounded admitted candidate metadata for explicit L2 expansion."""

    path: str
    digest: str
    qualified_name: str | None = None
    symbol_id: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    kind: Literal["file", "symbol"]

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return normalize_relative_path(value)


class RelationEvidence(_Model):
    """Verified one-hop relation metadata; never prompt material by itself."""

    edge_id: str
    direction: Literal["import", "reverse"]
    anchor_path: str
    anchor_digest: str
    related_path: str
    related_digest: str
    importer_path: str
    importer_digest: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @field_validator("anchor_path", "related_path", "importer_path")
    @classmethod
    def validate_paths(cls, value: str) -> str:
        return normalize_relative_path(value)

    @model_validator(mode="after")
    def validate_bounds(self) -> "RelationEvidence":
        if self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line")
        return self


class PackOmission(_Model):
    reason: str
    path: str | None = None
    detail: str | None = None


class PackMetrics(_Model):
    estimator: Literal["local_tokenizer", "conservative_estimate"] = (
        "conservative_estimate"
    )
    considered: int = Field(default=0, ge=0)
    selected: int = Field(default=0, ge=0)
    deduplicated: int = Field(default=0, ge=0)
    omitted_by_reason: dict[str, int] = Field(default_factory=dict)
    section_tokens: dict[str, int] = Field(default_factory=dict)
    section_limits: dict[str, int] = Field(default_factory=dict)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    remaining_total: int = Field(default=0, ge=0)
    expansion_count: int = Field(default=0, ge=0)
    cumulative_source_units: int = Field(default=0, ge=0)
    attempts: int = Field(default=0, ge=0)
    remaining_expansions: int = Field(default=0, ge=0)


class DependencyCoverageSummary(_Model):
    """Opt-in dependency closure counters; absent when the policy is disabled."""

    enabled: bool = False
    status: Literal["disabled", "complete", "incomplete", "refused"] = "disabled"
    estimator: Literal["conservative_estimate"] = "conservative_estimate"
    max_depth: int = Field(default=0, ge=0)
    max_definitions: int = Field(default=0, ge=0)
    max_tokens: int = Field(default=0, ge=0)
    effective_token_ceiling: int = Field(default=0, ge=0)
    definitions_added: int = Field(default=0, ge=0)
    imports_added: int = Field(default=0, ge=0)
    charged_tokens: int = Field(default=0, ge=0)
    coverage_complete: bool = True


class DependencyEvidenceRow(_Model):
    origin_path: str
    origin_start_line: int = Field(ge=1)
    origin_end_line: int = Field(ge=1)
    origin_digest: str
    target_path: str | None = None
    target_start_line: int | None = Field(default=None, ge=1)
    target_end_line: int | None = Field(default=None, ge=1)
    target_digest: str | None = None
    reference_line: int = Field(ge=1)
    relationship: str
    reason: str
    depth: int = Field(ge=0)
    estimated_unit_cost: int = Field(default=0, ge=0)
    newly_charged: bool = False


class DependencyIssueRow(_Model):
    origin_path: str
    origin_start_line: int = Field(ge=1)
    origin_end_line: int = Field(ge=1)
    origin_digest: str
    reason: str
    target_path: str | None = None
    target_start_line: int | None = Field(default=None, ge=1)
    target_end_line: int | None = Field(default=None, ge=1)
    target_digest: str | None = None


class PackedContextV2(_Model):
    """Versioned pack metadata plus the exact prompt it was counted from."""

    version: Literal[2] = 2
    level: PackLevel
    task_id: str
    status: PackStatus
    model: str
    thinking: Literal["enabled", "disabled"] = "disabled"
    tools: tuple[dict[str, Any], ...] = ()
    response_format: dict[str, Any] | None = None
    fragments: tuple[PackedFragment, ...] = ()
    map_rows: tuple[PackedMapRow, ...] = ()
    omissions: tuple[PackOmission, ...] = ()
    reason_codes: tuple[str, ...] = ()
    included_fragment_ids: tuple[str, ...] = ()
    prior_fragment_ids: tuple[str, ...] = ()
    source_revisions: tuple[SourceRevision, ...] = ()
    candidate_refs: tuple[CandidateRef, ...] = ()
    relation_evidence: tuple[RelationEvidence, ...] = ()
    map_revision: str | None = None
    scan_incomplete: bool = False
    degraded_reasons: tuple[str, ...] = ()
    messages: tuple[dict[str, Any], ...] = ()
    metrics: PackMetrics = Field(default_factory=PackMetrics)
    # Opt-in dependency coverage projection (defaults keep legacy dumps stable).
    dependency_coverage: DependencyCoverageSummary | None = None
    dependency_evidence: tuple[DependencyEvidenceRow, ...] = ()
    dependency_issues: tuple[DependencyIssueRow, ...] = ()

    @property
    def dispatchable(self) -> bool:
        return self.status == "ready" and bool(self.messages)


class ExpansionRequest(_Model):
    """Exactly one explicit L2 selector."""

    kind: Literal["symbol", "range", "dependency"]

    path: str | None = None
    expected_digest: str | None = None
    qualified_name: str | None = None
    occurrence: int | None = Field(default=None, ge=0)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    full_file: bool = False
    justification: str | None = None
    edge_id: str | None = None

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        return normalize_relative_path(value) if value is not None else None

    @field_validator("justification")
    @classmethod
    def validate_justification(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_text(value, what="justification")

    @model_validator(mode="after")
    def validate_selector(self) -> "ExpansionRequest":
        if self.kind == "symbol":
            if not self.path or not self.expected_digest or not self.qualified_name:
                raise ValueError("symbol expansion requires path, digest and qualified name")
            if self.edge_id is not None or self.start_line is not None:
                raise ValueError("symbol expansion cannot carry range or dependency fields")
        elif self.kind == "range":
            if not self.path or not self.expected_digest:
                raise ValueError("range expansion requires path and digest")
            if self.start_line is None or self.end_line is None:
                raise ValueError("range expansion requires inclusive bounds")
            if self.end_line < self.start_line:
                raise ValueError("end_line must be >= start_line")
            if self.full_file and not self.justification:
                raise ValueError("full-file expansion requires a justification")
            if self.qualified_name is not None or self.edge_id is not None:
                raise ValueError("range expansion cannot carry symbol or dependency fields")
        else:
            if not self.edge_id:
                raise ValueError("dependency expansion requires an edge ID")
            if any(
                value is not None
                for value in (
                    self.path,
                    self.expected_digest,
                    self.qualified_name,
                    self.start_line,
                    self.end_line,
                    self.justification,
                )
            ):
                raise ValueError("dependency expansion cannot carry path or range fields")
            if self.full_file:
                raise ValueError("dependency expansion cannot request a full file")
        return self


class DependencyProofLink(_Model):
    """Exact resolved dependency relationship frozen into an expansion receipt."""

    path: str
    digest: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    relationship: str
    depth: int = Field(ge=0)
    reference_line: int = Field(ge=1)


class ExpansionDelivery(_Model):
    """Private session-owned receipt for one verified L2 delta.

    When dependency coverage is enabled, receipts also bind the analyzer contract,
    policy caps, originating seed, and exact resolved relationships so delivery
    can re-prove against the original checked read universe even when fragment
    digests are unchanged.
    """

    receipt_id: str
    target_kind: Literal["symbol", "range", "dependency"]
    delta_ids: tuple[str, ...] = ()
    carry_forward_ids: tuple[str, ...] = ()
    envelope_fingerprint: str
    source_refs: tuple[FragmentRef, ...] = ()
    edge_id: str | None = None
    # Opt-in dependency proof binding (absent/empty when coverage is disabled).
    analyzer_contract_version: int | None = None
    dependency_max_depth: int | None = None
    dependency_max_definitions: int | None = None
    dependency_max_tokens: int | None = None
    seed_path: str | None = None
    seed_digest: str | None = None
    seed_start_line: int | None = Field(default=None, ge=1)
    seed_end_line: int | None = Field(default=None, ge=1)
    dependency_links: tuple[DependencyProofLink, ...] = ()


class PackResult(_Model):
    """Typed result; non-ready results never carry deliverable messages."""

    status: PackStatus
    pack: PackedContextV2 | None = None
    reason_codes: tuple[str, ...] = ()
    receipt_id: str | None = None

    @model_validator(mode="after")
    def validate_dispatchability(self) -> "PackResult":
        if self.status != "ready" and self.pack is not None and self.pack.messages:
            raise ValueError("non-ready results must not expose prompt messages")
        return self


def fragment_id_for(path: str, digest: str, start_line: int, end_line: int) -> str:
    payload = json.dumps(
        [path, digest, start_line, end_line],
        separators=(",", ":"),
        ensure_ascii=False,
        sort_keys=True,
    )
    return "frag:sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def edge_id_for(
    *,
    direction: str,
    anchor_path: str,
    anchor_digest: str,
    related_path: str,
    related_digest: str,
    importer_path: str,
    importer_digest: str,
    start_line: int,
    end_line: int,
) -> str:
    payload = json.dumps(
        [
            direction,
            anchor_path,
            anchor_digest,
            related_path,
            related_digest,
            importer_path,
            importer_digest,
            start_line,
            end_line,
        ],
        separators=(",", ":"),
        ensure_ascii=False,
        sort_keys=True,
    )
    return "edge:sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def require_ready(pack: PackedContextV2) -> None:
    if pack.status != "ready":
        raise InvalidContextRequest("A ready packed context is required")
