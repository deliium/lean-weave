"""Typed context models with deterministic, project-relative coordinates.

Repository-derived values are kept in their own fields so renderers can delimit
untrusted data from trusted constraints. Omissions are diagnostic only and are
never rendered into prompt messages.
"""

from __future__ import annotations

import hashlib
import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lean_weave.context.policy import contains_secret
from lean_weave.errors import ForbiddenContext, InvalidContextRequest

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ExpansionLevel(str, Enum):
    """Progressive context expansion, narrowest detail first."""

    MAP = "map"
    SYMBOLS = "symbols"
    RANGES = "ranges"
    FULL = "full"


class SymbolKind(str, Enum):
    CLASS = "class"
    FUNCTION = "function"
    ASYNC_FUNCTION = "async_function"
    METHOD = "method"
    ASYNC_METHOD = "async_method"


class OmissionReason(str, Enum):
    """Stable, safe codes explaining why a candidate was not admitted."""

    EXCLUDED = "excluded"
    HARD_DENIED = "hard_denied"
    SECRET = "secret"
    BINARY = "binary"
    UNDECODABLE = "undecodable"
    OVERSIZED = "oversized"
    SCAN_LIMIT = "scan_limit"
    UNREADABLE = "unreadable"
    NOT_ELIGIBLE = "not_eligible"
    UNSUPPORTED = "unsupported"
    PARSE_ERROR = "parse_error"
    BUDGET = "budget"
    DUPLICATE = "duplicate"
    SUPERSEDED = "superseded"
    OVERLAP = "overlap"
    NOT_FOUND = "not_found"
    MAP_LIMIT = "map_limit"
    NO_RECENCY = "no_recency"
    STAGE_SCOPE = "stage_scope"
    INVALID_RANGE = "invalid_range"
    EMPTY = "empty"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def content_digest(text: str) -> str:
    """Digest the canonical decoded snapshot (newlines already normalized)."""

    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def iter_lines(text: str) -> list[str]:
    """Return logical lines without inventing a final empty line."""

    if not text:
        return []
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    return lines


def normalize_relative_path(value: str) -> str:
    """Normalize a project-relative POSIX path and reject unsafe input."""

    if not isinstance(value, str) or not value.strip():
        raise InvalidContextRequest("Context path must be a non-empty string")
    if "\x00" in value or _CONTROL.search(value):
        raise InvalidContextRequest("Context path contains control characters")
    candidate = value.replace("\\", "/").strip()
    while candidate.startswith("./"):
        candidate = candidate[2:]
    if candidate.startswith("/"):
        raise InvalidContextRequest("Context path must be project-relative")
    parts = candidate.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise InvalidContextRequest("Context path must not contain empty or parent segments")
    return "/".join(parts)


def validate_text(value: str, *, what: str = "task text") -> str:
    """Reject empty, control-bearing, or secret-looking supplied text.

    The filter is deliberately narrow and never echoes the rejected value.
    """

    if not isinstance(value, str) or not value.strip():
        raise InvalidContextRequest(f"{what.capitalize()} must be non-empty")
    if "\x00" in value or _CONTROL.search(value):
        raise InvalidContextRequest(f"{what.capitalize()} contains control characters")
    if contains_secret(value):
        raise ForbiddenContext(f"{what.capitalize()} was rejected by the credential filter")
    return value


class SourceRange(_Model):
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return normalize_relative_path(value)

    @model_validator(mode="after")
    def validate_bounds(self) -> "SourceRange":
        if self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line")
        return self


class SymbolRef(_Model):
    path: str
    simple_name: str
    qualified_name: str
    kind: SymbolKind
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    signature: str | None = None
    parent: str | None = None

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return normalize_relative_path(value)

    @field_validator("simple_name", "qualified_name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value or _CONTROL.search(value):
            raise ValueError("symbol names must be non-empty without control characters")
        return value

    @model_validator(mode="after")
    def validate_bounds(self) -> "SymbolRef":
        if self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line")
        return self


class DigestEntry(_Model):
    path: str
    digest: str
    chars: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return normalize_relative_path(value)


class CodeExcerpt(_Model):
    """One admitted code unit with complete provenance.

    ``text`` is canonical code (indentation preserved). ``provenance`` lists the
    primary range first, then identical alternate ranges for de-duplicated text.
    """

    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    text: str
    digest: str
    kind: Literal["symbol", "range", "full", "partial"]
    symbol: str | None = None
    provenance: list[SourceRange] = Field(default_factory=list)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return normalize_relative_path(value)

    @model_validator(mode="after")
    def validate_bounds(self) -> "CodeExcerpt":
        if self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line")
        if not self.provenance:
            object.__setattr__(
                self,
                "provenance",
                [SourceRange(path=self.path, start_line=self.start_line, end_line=self.end_line)],
            )
        return self


class Omission(_Model):
    """Diagnostic-only record; never rendered into prompt messages."""

    reason: OmissionReason
    path: str | None = None
    detail: str | None = None


class ContextQuery(_Model):
    """A local preview or stage-scoped retrieval request."""

    task: str
    level: ExpansionLevel = ExpansionLevel.SYMBOLS
    paths: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    reason: str | None = None
    limit: int | None = Field(default=None, gt=0)
    include_tests: bool = False
    allow_git: bool = False
    constraints: list[str] = Field(default_factory=list)
    stage: str | None = None
    #: Optional hard scope: only these eligible paths may be ranked/admitted.
    scope_paths: list[str] = Field(default_factory=list)
    #: When true, only symbols matching an explicit hint are eligible.
    exact_symbols_only: bool = False
    # Only used by ``inspect`` for an explicit non-AST line interval.
    line_range: SourceRange | None = None

    @field_validator("task")
    @classmethod
    def validate_task(cls, value: str) -> str:
        return validate_text(value, what="task text")

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            normalized = normalize_relative_path(value)
            if normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
        return result

    @field_validator("scope_paths")
    @classmethod
    def validate_scope_paths(cls, values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            normalized = normalize_relative_path(value)
            if normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
        return result

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            if not value or not value.strip() or _CONTROL.search(value):
                raise ValueError("symbol hints must be non-empty without control characters")
            if value not in result:
                result.append(value)
        return result

    @field_validator("constraints")
    @classmethod
    def validate_constraints(cls, values: list[str]) -> list[str]:
        for value in values:
            validate_text(value, what="constraint")
        return values

    @model_validator(mode="after")
    def validate_level(self) -> "ContextQuery":
        if self.level == ExpansionLevel.FULL:
            if len(self.paths) != 1:
                raise ValueError("full expansion requires exactly one explicit path")
            if not self.reason or not self.reason.strip():
                raise ValueError("full expansion requires a non-empty justification")
        if self.line_range is not None and self.line_range.path not in self.paths:
            raise ValueError("line_range path must be one of the explicit paths")
        return self


class ContextPack(_Model):
    """Minimal, provenance-bearing context for one request or stage."""

    task: str
    level: ExpansionLevel
    stage: str | None = None
    constraints: list[str] = Field(default_factory=list)
    relevant_paths: list[str] = Field(default_factory=list)
    symbols: list[SymbolRef] = Field(default_factory=list)
    excerpts: list[CodeExcerpt] = Field(default_factory=list)
    repository_map: list[str] = Field(default_factory=list)
    digests: list[DigestEntry] = Field(default_factory=list)
    omissions: list[Omission] = Field(default_factory=list)
    estimated_tokens: int = Field(default=0, ge=0)
    required_tokens: int = Field(default=0, ge=0)
