"""Typed offline retrieval request and result contracts.

These models are additive preview surfaces. They do not extend ContextQuery,
ContextPack, or stage-scoped ContextProvider.build contracts.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lean_weave.context.models import normalize_relative_path
from lean_weave.context.policy import contains_secret
from lean_weave.errors import InvalidContextRequest
from lean_weave.logging_setup import get_logger

log = get_logger("context.retrieval_models")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

TaskKind = Literal["bugfix", "feature", "refactor", "test", "diagnose"]
ResultKind = Literal["all", "file", "symbol"]
HitKind = Literal["file", "symbol"]
SourceKind = Literal[
    "indexed_definition",
    "live_definition",
    "lexical_window",
    "file_head",
    "empty_file",
]
SignalName = Literal[
    "exact_symbol",
    "name",
    "lexical",
    "import_dependency",
    "reverse_dependency",
    "git_diff",
    "related_test",
    "map_importance",
]
DependencyDirection = Literal["import", "reverse"]
DegradedReason = Literal[
    "source_scan_incomplete",
    "source_changed",
    "source_unavailable",
    "index_missing",
    "index_stale",
    "index_incomplete",
    "index_unavailable",
    "lexical_python_fallback",
    "git_unavailable",
]

DEGRADED_REASON_ORDER: tuple[DegradedReason, ...] = (
    "source_scan_incomplete",
    "source_changed",
    "source_unavailable",
    "index_missing",
    "index_stale",
    "index_incomplete",
    "index_unavailable",
    "lexical_python_fallback",
    "git_unavailable",
)

_QUERY_MAX_CHARS = 2000
_GENERIC_QUERY_REJECT = "Retrieval query was rejected"


def _reject_query(reason: str) -> None:
    log.debug("[Context.retrieval] request rejected reason=%s", reason)
    raise InvalidContextRequest(_GENERIC_QUERY_REJECT)


def validate_retrieval_query(value: str) -> str:
    """Validate an explicit retrieval query without echoing rejected text."""

    if not isinstance(value, str):
        _reject_query("type")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        _reject_query("controls")
    stripped = value.strip()
    if not stripped:
        _reject_query("empty")
    if len(stripped) > _QUERY_MAX_CHARS:
        _reject_query("oversize")
    if not stripped.isprintable():
        _reject_query("nonprintable")
    if "\n" in stripped or "\r" in stripped:
        _reject_query("multiline")
    if contains_secret(stripped):
        _reject_query("credential")
    return stripped


class SignalReason(_Model):
    signal: SignalName
    value: int = Field(ge=0)
    weight: int = Field(ge=0, le=100)
    weighted_points: int = Field(ge=0)


class DependencyEvidence(_Model):
    direction: DependencyDirection
    related_path: str
    importer_path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    source_digest: str

    @field_validator("related_path", "importer_path")
    @classmethod
    def validate_paths(cls, value: str) -> str:
        return normalize_relative_path(value)

    @model_validator(mode="after")
    def validate_bounds(self) -> "DependencyEvidence":
        if self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line")
        return self


class RetrievalRequest(_Model):
    """Explicit whole-checkout retrieval preview request."""

    query: str
    task_kind: TaskKind = "feature"
    kind: ResultKind = "all"
    top_k: int | None = None

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        return validate_retrieval_query(value)

    @field_validator("top_k", mode="before")
    @classmethod
    def validate_top_k(cls, value: object) -> int | None:
        if value is None:
            return None
        if type(value) is not int:
            raise ValueError("top_k must be an integer")
        if value < 1 or value > 100:
            raise ValueError("top_k must be between 1 and 100")
        return value


class RetrievalHit(_Model):
    kind: HitKind
    path: str
    symbol_id: str | None = None
    qualified_name: str | None = None
    definition_start_line: int | None = None
    start_line: int | None = None
    end_line: int | None = None
    source_digest: str
    source_kind: SourceKind
    relevance_score: int = Field(ge=0, le=1000)
    ranking_reasons: tuple[SignalReason, ...] = ()
    related_dependencies: tuple[DependencyEvidence, ...] = ()
    estimated_token_cost: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return normalize_relative_path(value)

    @model_validator(mode="after")
    def validate_hit(self) -> "RetrievalHit":
        empty = self.source_kind == "empty_file"
        if empty:
            if self.start_line is not None or self.end_line is not None:
                raise ValueError("empty_file hits must omit line ranges")
        else:
            if self.start_line is None or self.end_line is None:
                raise ValueError("non-empty hits require inclusive line ranges")
            if self.end_line < self.start_line:
                raise ValueError("end_line must be >= start_line")
        if self.kind == "symbol":
            if self.definition_start_line is None:
                raise ValueError("symbol hits require definition_start_line")
            if self.definition_start_line < 1:
                raise ValueError("definition_start_line must be >= 1")
        elif self.definition_start_line is not None:
            raise ValueError("file hits must omit definition_start_line")
        return self


class RetrievalResult(_Model):
    hits: tuple[RetrievalHit, ...] = ()
    task_kind: TaskKind
    kind: ResultKind
    top_k: int = Field(ge=1, le=100)
    degraded_reasons: tuple[DegradedReason, ...] = ()
    truncated: bool = False

    @model_validator(mode="after")
    def validate_degraded_order(self) -> "RetrievalResult":
        if len(self.degraded_reasons) != len(set(self.degraded_reasons)):
            raise ValueError("degraded_reasons must be unique")
        order = {code: index for index, code in enumerate(DEGRADED_REASON_ORDER)}
        previous = -1
        for code in self.degraded_reasons:
            rank = order[code]
            if rank < previous:
                raise ValueError("degraded_reasons must follow the stable order")
            previous = rank
        return self


class RetrievalProvider(Protocol):
    """Structural protocol for callers that only need offline retrieval."""

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        """Return ranked source references for an explicit preview request."""
        ...
