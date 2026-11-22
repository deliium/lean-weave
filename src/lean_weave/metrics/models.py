"""Typed usage-accounting records and the attempt-journal boundary.

The journal stores accounting metadata only. It never stores prompts, provider
bodies, headers, tool payloads, or reasoning text.
"""

from __future__ import annotations

import json
import math
from typing import Any, Literal, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lean_weave.contracts._types import ContextStrategy, GatewayStage
from lean_weave.schemas.models import TokenUsage

EVENT_VERSION = 1
MAX_USAGE_BYTES = 16_384
MAX_USAGE_DEPTH = 6
MAX_ATTRIBUTION_CHARS = 128

_SECRET_KEYS = ("api_key", "apikey", "authorization", "password", "secret", "credential")


class AccountingModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UsageAttribution(AccountingModel):
    """Trusted local attribution for one gateway dispatch.

    These values never come from model text. ``task_id`` follows the existing
    plan ID contract, ``strategy`` names the actual context selector, and
    ``stage_attempt`` is the workflow stage-attempt ordinal (1-based).
    """

    task_id: str | None = Field(default=None, max_length=MAX_ATTRIBUTION_CHARS)
    strategy: ContextStrategy | None = None
    stage_attempt: int | None = Field(default=None, ge=1)

    @field_validator("task_id")
    @classmethod
    def validate_task_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("invalid task attribution")
        return value


def parse_usage_attribution(value: Any) -> UsageAttribution | None:
    """Validate untrusted local attribution; return ``None`` when absent.

    Malformed attribution is rejected by the gateway before any booking.
    """

    if value is None:
        return None
    if isinstance(value, UsageAttribution):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("usage attribution must be a mapping")
    return UsageAttribution.model_validate(dict(value))


class UsageCounters(AccountingModel):
    """Normalized counters where ``None`` means "not reported", not zero."""

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cache_hit_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)

    def core_complete(self) -> bool:
        return (
            self.input_tokens is not None
            and self.output_tokens is not None
            and self.total_tokens is not None
            and self.total_tokens == self.input_tokens + self.output_tokens
        )

    def measured_total(self) -> int | None:
        if self.total_tokens is not None:
            return self.total_tokens
        if self.input_tokens is not None and self.output_tokens is not None:
            return self.input_tokens + self.output_tokens
        return None


class ReportedUsage(AccountingModel):
    """Provider-reported usage with the decoded object preserved intact."""

    raw: dict[str, Any]
    counters: UsageCounters
    measured: bool = True


class JournalEvent(AccountingModel):
    version: int = EVENT_VERSION
    event: Literal["reservation", "outcome", "legacy_usage"]
    event_id: str = Field(min_length=1, max_length=512)
    reservation_id: str | None = Field(default=None, max_length=512)
    request_id: str = Field(min_length=1, max_length=512)
    attempt_id: str = Field(min_length=1, max_length=512)
    attempt_ordinal: int = Field(default=0, ge=0)
    feature_id: str = Field(min_length=1, max_length=512)
    stage: str = Field(min_length=1, max_length=32)
    model_requested: str = Field(min_length=1, max_length=256)
    model_returned: str | None = Field(default=None, max_length=256)
    created_at: str = Field(min_length=1, max_length=64)
    estimated_input_tokens: int | None = Field(default=None, ge=0)
    output_cap: int | None = Field(default=None, ge=0)
    charge_tokens: int | None = Field(default=None, ge=0)
    reservation_charge: int | None = Field(default=None, ge=0)
    usage: ReportedUsage | None = None
    status: str | None = Field(default=None, max_length=32)
    error_code: str | None = Field(default=None, max_length=64)
    duration_ms: int | None = Field(default=None, ge=0)
    finish_reason: str | None = Field(default=None, max_length=64)
    attribution: UsageAttribution | None = None
    legacy: bool = False


class BudgetReservation(AccountingModel):
    reservation_id: str
    request_id: str
    attempt_id: str
    attempt_ordinal: int = Field(ge=0)
    feature_id: str
    stage: GatewayStage
    model_requested: str
    created_at: str
    estimated_input_tokens: int = Field(ge=0)
    output_cap: int = Field(ge=0)
    charge_tokens: int = Field(ge=0)
    attribution: UsageAttribution | None = None


class UsagePresence(AccountingModel):
    input_tokens: bool = False
    output_tokens: bool = False
    total_tokens: bool = False
    cache_hit_tokens: bool = False
    reasoning_tokens: bool = False

    def merge(self, counters: UsageCounters) -> "UsagePresence":
        return UsagePresence(
            input_tokens=self.input_tokens or counters.input_tokens is not None,
            output_tokens=self.output_tokens or counters.output_tokens is not None,
            total_tokens=self.total_tokens or counters.total_tokens is not None,
            cache_hit_tokens=self.cache_hit_tokens or counters.cache_hit_tokens is not None,
            reasoning_tokens=self.reasoning_tokens or counters.reasoning_tokens is not None,
        )


class AttemptSummary(AccountingModel):
    reservation_id: str | None = None
    request_id: str
    attempt_id: str
    attempt_ordinal: int
    is_retry: bool
    stage: str
    task_id: str | None = None
    strategy: ContextStrategy | None = None
    stage_attempt: int | None = None
    model_requested: str
    model_returned: str | None = None
    status: str
    error_code: str | None = None
    finish_reason: str | None = None
    duration_ms: int | None = None
    charge_tokens: int
    measured_charge: int = 0
    estimated_charge: int = 0
    measured: bool = False
    counters: UsageCounters = Field(default_factory=UsageCounters)
    # Reservation preflight inputs (read-only projection; never invent zeros).
    estimated_input_tokens: int | None = Field(default=None, ge=0)
    output_cap: int | None = Field(default=None, ge=0)


class ModelUsageSummary(AccountingModel):
    model_requested: str
    model_returned: str | None = None
    measured: UsageCounters = Field(default_factory=UsageCounters)
    presence: UsagePresence = Field(default_factory=UsagePresence)
    attempts: int = 0
    measured_charge: int = 0
    estimated_charge: int = 0


class StageUsageSummary(AccountingModel):
    stage: str
    measured: UsageCounters = Field(default_factory=UsageCounters)
    presence: UsagePresence = Field(default_factory=UsagePresence)
    models: list[ModelUsageSummary] = Field(default_factory=list)
    attempts: int = 0
    retry_sends: int = 0
    failed_attempts: int = 0
    duration_ms: int = 0
    measured_charge: int = 0
    estimated_charge: int = 0


class FeatureUsageSummary(AccountingModel):
    feature_id: str
    measured: TokenUsage = Field(default_factory=TokenUsage)
    counters: UsageCounters = Field(default_factory=UsageCounters)
    presence: UsagePresence = Field(default_factory=UsagePresence)
    attempts: list[AttemptSummary] = Field(default_factory=list)
    stages: list[StageUsageSummary] = Field(default_factory=list)
    models: list[ModelUsageSummary] = Field(default_factory=list)
    attempts_total: int = 0
    retry_sends: int = 0
    failed_attempts: int = 0
    unresolved_attempts: int = 0
    budget_charge: int = 0
    measured_charge: int = 0
    estimated_charge: int = 0
    unresolved_charge: int = 0


class AttemptRecord(AccountingModel):
    """One canonical reservation/outcome pair for reporting.

    Unresolved reservations have ``status == "unresolved"`` and no outcome
    counters. Optional counters remain ``None`` when the provider omitted them.
    """

    feature_id: str
    reservation_id: str | None = None
    request_id: str
    attempt_id: str
    attempt_ordinal: int = Field(ge=0)
    stage: str
    task_id: str | None = None
    strategy: ContextStrategy | None = None
    stage_attempt: int | None = None
    model_requested: str
    model_returned: str | None = None
    status: str
    error_code: str | None = None
    finish_reason: str | None = None
    duration_ms: int | None = None
    counters: UsageCounters = Field(default_factory=UsageCounters)
    charge_tokens: int = Field(default=0, ge=0)
    measured: bool = False
    estimated_input_tokens: int | None = Field(default=None, ge=0)
    output_cap: int | None = Field(default=None, ge=0)
    legacy: bool = False

    @property
    def measured_total(self) -> int | None:
        return self.counters.measured_total()


class ReportingSnapshot(AccountingModel):
    """Read-only projection of one feature's journal evidence."""

    feature_id: str
    case_id: str | None = None
    available: bool = True
    attempts: list[AttemptRecord] = Field(default_factory=list)
    legacy_charge: int = Field(default=0, ge=0)
    budget_charge: int = Field(default=0, ge=0)
    measured_charge: int = Field(default=0, ge=0)
    estimated_charge: int = Field(default=0, ge=0)
    unresolved_charge: int = Field(default=0, ge=0)
    spent: int = Field(default=0, ge=0)
    presence: UsagePresence = Field(default_factory=UsagePresence)
    limit_reason: str | None = None


class AttemptJournal(Protocol):
    """Narrow accounting boundary used by the gateway."""

    def reserve_attempt(
        self,
        *,
        feature_id: str,
        stage: GatewayStage,
        model_requested: str,
        request_id: str,
        attempt_id: str,
        attempt_ordinal: int,
        estimated_input_tokens: int,
        output_cap: int,
        budget_tokens: int,
    ) -> BudgetReservation:
        ...

    def finalize_attempt(
        self,
        reservation: BudgetReservation,
        *,
        status: str,
        usage: ReportedUsage | None = None,
        model_returned: str | None = None,
        duration_ms: int | None = None,
        finish_reason: str | None = None,
        error_code: str | None = None,
    ) -> JournalEvent:
        ...

    def spent(self, feature_id: str) -> int:
        ...

    def summary(self, feature_id: str) -> FeatureUsageSummary:
        ...


class AttributedAttemptJournal(AttemptJournal, Protocol):
    """Optional journal capability: reserve with trusted attribution."""

    def reserve_attributed_attempt(
        self,
        *,
        feature_id: str,
        stage: GatewayStage,
        model_requested: str,
        request_id: str,
        attempt_id: str,
        attempt_ordinal: int,
        estimated_input_tokens: int,
        output_cap: int,
        budget_tokens: int,
        attribution: UsageAttribution,
    ) -> BudgetReservation:
        ...


def _json_safe(value: Any, depth: int = 0) -> bool:
    if depth > MAX_USAGE_DEPTH:
        return False
    if value is None or isinstance(value, (bool, int, str)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Mapping):
        return all(
            isinstance(key, str) and _json_safe(item, depth + 1)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return all(_json_safe(item, depth + 1) for item in value)
    return False


def _secret_bearing(value: Any, depth: int = 0) -> bool:
    if depth > MAX_USAGE_DEPTH:
        return False
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(secret in normalized for secret in _SECRET_KEYS):
                return True
            if _secret_bearing(item, depth + 1):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_secret_bearing(item, depth + 1) for item in value)
    return False


def _as_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _details(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key)
    return value if isinstance(value, Mapping) else {}


def normalize_usage(raw: Any) -> ReportedUsage | None:
    """Normalize provider usage without inventing measured zeros.

    Invalid, oversized, deeply nested, or secret-bearing objects are rejected
    as a whole; callers then charge conservatively.
    """
    if not isinstance(raw, Mapping):
        if hasattr(raw, "model_dump"):
            try:
                raw = raw.model_dump(exclude_none=False)
            except (TypeError, ValueError):
                return None
        else:
            return None
    if not _json_safe(raw) or _secret_bearing(raw):
        return None
    try:
        encoded = json.dumps(dict(raw), separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError, RecursionError):
        return None
    if len(encoded.encode("utf-8")) > MAX_USAGE_BYTES:
        return None

    prompt_details = _details(raw, "prompt_tokens_details")
    completion_details = _details(raw, "completion_tokens_details")
    cache = _as_count(raw.get("prompt_cache_hit_tokens"))
    if cache is None:
        cache = _as_count(prompt_details.get("cached_tokens"))
    counters = UsageCounters(
        input_tokens=_as_count(raw.get("prompt_tokens")),
        output_tokens=_as_count(raw.get("completion_tokens")),
        total_tokens=_as_count(raw.get("total_tokens")),
        cache_hit_tokens=cache,
        reasoning_tokens=_as_count(completion_details.get("reasoning_tokens")),
    )
    return ReportedUsage(raw=dict(raw), counters=counters)


def token_usage_from_counters(counters: UsageCounters) -> TokenUsage:
    """Compatibility view: measured counters only, unknown fields become zero."""
    total = counters.measured_total()
    return TokenUsage(
        prompt_tokens=counters.input_tokens or 0,
        completion_tokens=counters.output_tokens or 0,
        total_tokens=total or 0,
        reasoning_tokens=counters.reasoning_tokens or 0,
        prompt_cache_hit_tokens=counters.cache_hit_tokens or 0,
    )
