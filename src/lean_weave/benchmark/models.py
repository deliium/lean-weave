"""Versioned benchmark run schema shared by CLI, reports and gates."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

EvidenceSource = Literal[
    "offline_scripted",
    "local_estimate",
    "deepseek_live",
    "historical",
]
SuiteName = Literal["workflow", "retrieval"]

BENCHMARK_SCHEMA_VERSION = 1


@dataclass
class ArmRow:
    arm_id: str
    suite: str
    case_id: str
    variant: str
    repetition: int
    expected_outcome: str
    observed_outcome: str | None = None
    status: str = "not_started"
    reason: str = ""
    correct: bool | None = None
    oracle_ok: bool | None = None
    expected_negative: bool = False
    feature_id: str | None = None
    provenance: str = "offline_scripted"
    fixture_fingerprint: str | None = None
    origin_head: str | None = None
    conditions: dict[str, Any] = field(default_factory=dict)
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_hit_tokens: int | None = None
    reasoning_tokens: int | None = None
    estimated_input_tokens: int | None = None
    budget_charge: int | None = None
    coverage: dict[str, bool] = field(default_factory=dict)
    logical_calls: int = 0
    retry_attempts: int = 0
    verified_tasks: int = 0
    task_tokens: int | None = None
    workflow_retries: int | None = None
    failed_attempts: int = 0
    unresolved_attempts: int = 0
    verification_status: str | None = None
    verification_blocking: bool | None = None
    duration_ms: int | None = None
    active_ms: int | None = None
    error_class: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    retrieval: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BenchmarkRun:
    schema_version: int
    evidence_source: EvidenceSource
    suite: str
    started_at: str
    completed_at: str | None = None
    revision: str | None = None
    source_revision: str | None = None
    spec: dict[str, Any] = field(default_factory=dict)
    condition_fingerprint: str | None = None
    rows: list[ArmRow] = field(default_factory=list)
    incomplete: bool = False
    notes: list[str] = field(default_factory=list)
    live_skipped_reason: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "evidence_source": self.evidence_source,
            "suite": self.suite,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "revision": self.revision,
            "source_revision": self.source_revision,
            "spec": self.spec,
            "condition_fingerprint": self.condition_fingerprint,
            "rows": [row.to_dict() for row in self.rows],
            "incomplete": self.incomplete,
            "notes": list(self.notes),
            "live_skipped_reason": self.live_skipped_reason,
            "metrics": dict(self.metrics),
        }


@dataclass(frozen=True)
class WorkflowVariant:
    name: str
    context_mode: str
    context_extra: dict[str, Any] = field(default_factory=dict)

    def conditions(self) -> dict[str, Any]:
        return {
            "context_mode": self.context_mode,
            **{f"context.{key}": value for key, value in self.context_extra.items()},
        }


WORKFLOW_VARIANTS: tuple[WorkflowVariant, ...] = (
    WorkflowVariant("v1", "v1"),
    WorkflowVariant("v2", "v2-with-v1-fallback"),
    WorkflowVariant(
        "v2-map-off",
        "v2-with-v1-fallback",
        {"packing": {"map_tokens": 0}},
    ),
    WorkflowVariant(
        "v2-tight",
        "v2-with-v1-fallback",
        {"packing": {"source_tokens": 512}},
    ),
    WorkflowVariant(
        "v2-cache-cold",
        "v2-with-v1-fallback",
        {"cache": {"enabled": True}},
    ),
)
WORKFLOW_VARIANT_NAMES = tuple(item.name for item in WORKFLOW_VARIANTS)


def workflow_variant(name: str) -> WorkflowVariant:
    for item in WORKFLOW_VARIANTS:
        if item.name == name:
            return item
    raise ValueError(f"unknown_workflow_variant:{name}")
