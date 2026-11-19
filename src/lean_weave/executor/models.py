"""Strict request and result schemas for standalone code tools.

Validation failures serialize as canned status codes. Rejected input, raw
exception text, diffs, and file bodies never appear on a failure result.
``revision`` is the SHA-256 of the original file bytes and is distinct from
context-rendered content digests.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from lean_weave.logging_setup import get_logger

log = get_logger("executor.models")

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")

GUIDANCE: dict[str, str] = {
    "ok": "",
    "invalid": "The request is invalid. Correct the fields and retry without repeating rejected input.",
    "denied": "The path is outside the approved scope. Choose an authorized path.",
    "missing": "The path does not exist in the workspace. Refresh and retry.",
    "ambiguous": "The name matches more than one symbol. Retry with the qualified name.",
    "unsupported": "The file or patch form is not supported. No file was changed.",
    "oversized": "The requested unit exceeds the size budget. Request a narrower line range.",
    "incomplete": "The operation did not inspect every candidate. Do not treat an empty result as definitive.",
    "conflict": "The patch context does not match the file. Re-read the current revision and retry.",
    "stale": "The file revision is stale. Re-read the file and retry with the new revision.",
    "secret": "The credential filter rejected the content. Do not retry the same content.",
    "indeterminate": "Ignore policy for this subtree is unreadable. Do not guess which paths were omitted.",
    "unreadable": "The file could not be read safely. No content was returned.",
    "hard_link": "The target is linked from more than one name. No file was changed.",
    "identity": "The file identity changed before the edit. Re-read the current revision and retry.",
    "parse_error": "The Python file could not be parsed. No symbol body was returned.",
    "no_checks": "No checks are approved. This result is not a pass.",
    "unknown_check": "A check id is not approved. No checks were started.",
    "duplicate_check": "The check list contains duplicates. No checks were started.",
    "isolation_unavailable": "Workspace-code execution requires an offline isolation backend. The check was not started.",
    "blocked": "A selected check cannot run safely. No checks were started.",
    "timed_out": "The check exceeded its deadline and was terminated.",
    "output_limited": "The check exceeded its output budget and was terminated.",
    "not_run": "This check did not start.",
    "failed": "A check failed. Use the structured diagnostics.",
    "passed": "The approved checks completed successfully.",
    "missing_baseline": "This Git view has no comparison baseline. Do not treat it as a clean diff.",
    "unavailable": "Git metadata is missing or unsupported. No diff was read.",
    "clean": "The selected Git view has no changes.",
    "filtered": "The selected Git view has no authorized visible entries.",
    "changes": "The selected Git view has authorized changes.",
    "committed": "The edit was written.",
    "committed_unconfirmed": "The edit was written but durability could not be confirmed. Re-read the new revision before another edit.",
}


class _Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def file_revision(data: bytes) -> str:
    """SHA-256 of the original file bytes, not the decoded text."""

    return "sha256:" + hashlib.sha256(data).hexdigest()


def normalize_tool_path(value: str) -> str:
    """Return a project-relative POSIX path or raise ValueError with a generic message."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid path")
    if _CONTROL.search(value):
        raise ValueError("invalid path")
    candidate = value.replace("\\", "/").strip()
    while candidate.startswith("./"):
        candidate = candidate[2:]
    if candidate.startswith("/"):
        raise ValueError("invalid path")
    parts = candidate.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("invalid path")
    return "/".join(parts)


def _path(value: str) -> str:
    return normalize_tool_path(value)


def _name(value: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError("invalid name")
    return value


class SymbolLocation(_Schema):
    path: str
    qualified_name: str
    kind: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _path(value)

    @field_validator("qualified_name", "kind")
    @classmethod
    def validate_token(cls, value: str) -> str:
        if not value or _CONTROL.search(value):
            raise ValueError("invalid symbol")
        return value

    @model_validator(mode="after")
    def validate_bounds(self) -> "SymbolLocation":
        if self.end_line < self.start_line:
            raise ValueError("invalid range")
        return self


class ReadRangeRequest(_Schema):
    path: str
    start_line: int = Field(ge=1, le=1_000_000)
    end_line: int = Field(ge=1, le=1_000_000)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _path(value)

    @model_validator(mode="after")
    def validate_bounds(self) -> "ReadRangeRequest":
        if self.end_line < self.start_line:
            raise ValueError("invalid range")
        return self


class ReadSymbolRequest(_Schema):
    path: str
    name: str

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _path(value)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _name(value)


class SearchSymbolsRequest(_Schema):
    query: str
    paths: list[str] = Field(default_factory=list, max_length=100)
    hit_limit: int | None = Field(default=None, ge=1, le=1000)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        return _name(value)

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, values: list[str]) -> list[str]:
        return [_path(value) for value in values]


class ApplyPatchRequest(_Schema):
    path: str
    expected_revision: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    diff: str = Field(min_length=1, max_length=1_000_000)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _path(value)

    @field_validator("diff")
    @classmethod
    def validate_diff(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("invalid diff")
        return value


class InspectGitDiffRequest(_Schema):
    mode: Literal["staged", "unstaged"]
    paths: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, values: list[str]) -> list[str]:
        return [_path(value) for value in values]


class ReadRangeSuccess(_Schema):
    ok: Literal[True] = True
    status: Literal["ok"] = "ok"
    guidance: str = ""
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    text: str
    revision: str
    encoding: str
    newline: str


class ToolFailure(_Schema):
    """Failure envelope. It has no text, diff, or raw-error field."""

    ok: Literal[False] = False
    status: str
    guidance: str
    start_line: int | None = None
    end_line: int | None = None
    candidates: list[SymbolLocation] = Field(default_factory=list)
    hits: list[SymbolLocation] = Field(default_factory=list)
    complete: bool | None = None
    truncated: bool = False
    omissions: list[str] = Field(default_factory=list)
    limit: int | None = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in GUIDANCE or value == "ok":
            raise ValueError("invalid status")
        return value


class ReadSymbolSuccess(_Schema):
    ok: Literal[True] = True
    status: Literal["ok"] = "ok"
    guidance: str = ""
    path: str
    qualified_name: str
    kind: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    text: str
    revision: str
    encoding: str
    newline: str


class SearchSymbolsSuccess(_Schema):
    ok: Literal[True] = True
    status: Literal["ok", "incomplete"] = "ok"
    guidance: str = ""
    hits: list[SymbolLocation] = Field(default_factory=list)
    complete: bool = True
    truncated: bool = False
    omissions: list[str] = Field(default_factory=list)


class ApplyPatchSuccess(_Schema):
    ok: Literal[True] = True
    status: Literal["committed"] = "committed"
    guidance: str = ""
    revision: str
    lines_added: int = Field(ge=0)
    lines_removed: int = Field(ge=0)
    durability: Literal["confirmed", "unconfirmed"]


class GitPathEntry(_Schema):
    path: str
    status: Literal["A", "M", "D", "T"]

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _path(value)


class GitDiffSuccess(_Schema):
    ok: bool
    status: Literal["clean", "changes", "filtered", "incomplete", "unavailable", "missing_baseline"]
    guidance: str
    entries: list[GitPathEntry] = Field(default_factory=list)
    truncated: bool = False


def failure(status: str, **kwargs) -> ToolFailure:
    log.debug("[Tools.validate] outcome=%s", status)
    return ToolFailure(status=status, guidance=GUIDANCE[status], **kwargs)


def parse_model(model, value):
    """Validate ``value`` into ``model`` or return a canned failure."""

    if isinstance(value, model):
        return value
    try:
        return model.model_validate(value)
    except (ValidationError, ValueError, TypeError):
        log.debug("[Tools.validate] outcome=invalid model=%s", getattr(model, "__name__", "request"))
        return failure("invalid")


def cap_result_items(result, field: str, budget: int):
    """Trim repeated result items until the serialized envelope fits ``budget``.

    The item count is already bounded by the caller. This second bound keeps
    long path/name lists inside the configured character budget and marks the
    result ``truncated`` instead of silently exceeding it.
    """

    items = list(getattr(result, field))
    original = len(items)
    while items and len(result.model_dump_json()) > budget:
        items.pop()
        result = result.model_copy(update={field: items, "truncated": True})
    if original != len(items):
        log.debug(
            "[FIX] result cap field=%s kept=%s original=%s budget=%s",
            field,
            len(items),
            original,
            budget,
        )
    return result


def safe_validation_payload(exc: BaseException) -> dict[str, str]:
    """Serialize a validation error without input, locations, or exception text."""

    _ = exc
    log.debug("[Tools.validate] outcome=invalid")
    return {"ok": False, "status": "invalid", "guidance": GUIDANCE["invalid"]}
