"""Strict schemas for approved check batches.

Results carry stable codes and template summaries. They never carry raw
stdout, stderr, argv, or the rejected request payload.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lean_weave.executor.models import GUIDANCE, failure
from lean_weave.logging_setup import get_logger

log = get_logger("verify.models")


class _Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class RunChecksRequest(_Schema):
    """``check_ids`` omitted means the approved default list."""

    check_ids: list[str] | None = None

    @field_validator("check_ids")
    @classmethod
    def validate_ids(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        for item in values:
            if not isinstance(item, str) or not item or any(ord(char) < 32 for char in item):
                raise ValueError("invalid check id")
            if len(item) > 64:
                raise ValueError("invalid check id")
        return list(values)


class CheckDiagnostic(_Schema):
    code: str
    summary: str
    path: str | None = None
    line: int | None = Field(default=None, ge=1)

    @field_validator("code", "summary")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("invalid diagnostic")
        return value


class CheckItem(_Schema):
    check_id: str
    status: Literal["passed", "failed", "timed_out", "output_limited", "blocked", "not_run"]
    exit_code: int | None = None
    diagnostics: list[CheckDiagnostic] = Field(default_factory=list)
    diagnostic_omissions: int = Field(default=0, ge=0)


class RunChecksResult(_Schema):
    ok: bool
    status: str
    guidance: str
    items: list[CheckItem] = Field(default_factory=list)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in GUIDANCE:
            raise ValueError("invalid status")
        return value


def checks_failure(status: str) -> RunChecksResult:
    log.debug("[Tools.checks] outcome=%s", status)
    canned = failure(status)
    return RunChecksResult(ok=False, status=canned.status, guidance=canned.guidance, items=[])
