"""Pure, read-only aggregation over canonical journal reporting rows.

Nothing here books, settles, retries, or writes. Optional provider counters
remain ``None`` when absent; cache and reasoning subsets never add to the
processed total. Legacy standalone usage and probe rows are kept separately.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from lean_weave.contracts._types import PROBE_FEATURE_ID, PROBE_STAGE
from lean_weave.metrics.models import AttemptRecord, ReportingSnapshot

GroupDimension = Literal["feature", "task", "stage", "model", "strategy", "case"]
DEFAULT_GROUP_BY: tuple[GroupDimension, ...] = (
    "feature",
    "task",
    "stage",
    "model",
    "strategy",
    "case",
)
_NULL = "unknown"


@dataclass
class Coverage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    reasoning_tokens: int = 0
    attempts: int = 0

    def complete(self) -> dict[str, bool]:
        total = max(0, self.attempts)
        return {
            "input_tokens": total > 0 and self.input_tokens == total,
            "output_tokens": total > 0 and self.output_tokens == total,
            "total_tokens": total > 0 and self.total_tokens == total,
            "cache_hit_tokens": total > 0 and self.cache_hit_tokens == total,
            "reasoning_tokens": total > 0 and self.reasoning_tokens == total,
        }


@dataclass
class AggregateRow:
    dimensions: dict[str, str | None]
    attempts: int = 0
    logical_calls: int = 0
    finalized_attempts: int = 0
    failed_attempts: int = 0
    unresolved_attempts: int = 0
    retry_attempts: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_hit_tokens: int | None = None
    reasoning_tokens: int | None = None
    derived_totals: int = 0
    reported_totals: int = 0
    contradictory_totals: int = 0
    measured_charge: int = 0
    estimated_charge: int = 0
    unresolved_charge: int = 0
    budget_charge: int = 0
    duration_ms: int = 0
    coverage: Coverage = field(default_factory=Coverage)
    logical_keys: set[tuple[str, str]] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("logical_keys", None)
        payload["coverage"] = {
            **asdict(self.coverage),
            "complete": self.coverage.complete(),
        }
        return payload


@dataclass
class AnalyticsReport:
    schema_version: int
    feature_id: str
    group_by: tuple[str, ...]
    rows: list[AggregateRow] = field(default_factory=list)
    totals: AggregateRow | None = None
    legacy_charge: int = 0
    budget_charge: int = 0
    spent: int = 0
    measured_charge: int = 0
    estimated_charge: int = 0
    unresolved_charge: int = 0
    available: bool = True
    limit_reason: str | None = None
    presence: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "feature_id": self.feature_id,
            "group_by": list(self.group_by),
            "rows": [row.to_dict() for row in self.rows],
            "totals": None if self.totals is None else self.totals.to_dict(),
            "legacy_charge": self.legacy_charge,
            "budget_charge": self.budget_charge,
            "spent": self.spent,
            "measured_charge": self.measured_charge,
            "estimated_charge": self.estimated_charge,
            "unresolved_charge": self.unresolved_charge,
            "available": self.available,
            "limit_reason": self.limit_reason,
            "presence": dict(self.presence),
            "reconciliation": {
                "budget_charge": self.budget_charge,
                "component_sum": (
                    self.measured_charge
                    + self.estimated_charge
                    + self.unresolved_charge
                    + self.legacy_charge
                ),
                "spent": self.spent,
            },
        }


def _add(total: int | None, value: int | None) -> int | None:
    if value is None:
        return total
    return value if total is None else total + value


def _key(record: AttemptRecord, dimension: GroupDimension) -> str | None:
    if dimension == "feature":
        return record.feature_id
    if dimension == "task":
        return record.task_id
    if dimension == "stage":
        return record.stage
    if dimension == "model":
        if record.model_returned:
            return f"{record.model_requested}->{record.model_returned}"
        return f"{record.model_requested}->{_NULL}"
    if dimension == "strategy":
        return record.strategy
    if dimension == "case":
        return None
    return None


def _row_dimensions(
    record: AttemptRecord,
    group_by: Sequence[GroupDimension],
    case_id: str | None,
) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for dimension in group_by:
        value = case_id if dimension == "case" else _key(record, dimension)
        result[dimension] = value if value is not None else _NULL
    return result


def _merge(group_by: Sequence[GroupDimension], dims: dict[str, str | None]) -> tuple:
    return tuple(dims.get(dimension) for dimension in group_by)


def _row_for(
    rows: dict[tuple, AggregateRow],
    record: AttemptRecord,
    group_by: Sequence[GroupDimension],
    case_id: str | None,
) -> AggregateRow:
    dims = _row_dimensions(record, group_by, case_id)
    key = _merge(group_by, dims)
    row = rows.get(key)
    if row is None:
        row = AggregateRow(dimensions=dims)
        rows[key] = row
    return row


def _add_record(row: AggregateRow, record: AttemptRecord) -> None:
    row.attempts += 1
    row.coverage.attempts += 1
    row.logical_keys.add((record.feature_id, record.request_id))
    row.logical_calls = len(row.logical_keys)
    if record.attempt_ordinal > 0:
        row.retry_attempts += 1
    if record.status == "unresolved":
        row.unresolved_attempts += 1
        row.unresolved_charge += record.charge_tokens
        row.budget_charge += record.charge_tokens
        return
    row.finalized_attempts += 1
    if record.status != "success":
        row.failed_attempts += 1
    if record.measured:
        row.measured_charge += record.charge_tokens
    else:
        row.estimated_charge += record.charge_tokens
    row.budget_charge += record.charge_tokens
    if record.duration_ms is not None:
        row.duration_ms += record.duration_ms

    counters = record.counters
    if counters.input_tokens is not None:
        row.coverage.input_tokens += 1
        row.input_tokens = _add(row.input_tokens, counters.input_tokens)
    if counters.output_tokens is not None:
        row.coverage.output_tokens += 1
        row.output_tokens = _add(row.output_tokens, counters.output_tokens)
    if counters.cache_hit_tokens is not None:
        row.coverage.cache_hit_tokens += 1
        row.cache_hit_tokens = _add(row.cache_hit_tokens, counters.cache_hit_tokens)
    if counters.reasoning_tokens is not None:
        row.coverage.reasoning_tokens += 1
        row.reasoning_tokens = _add(row.reasoning_tokens, counters.reasoning_tokens)
    if counters.total_tokens is not None:
        row.coverage.total_tokens += 1
        row.reported_totals += 1
        row.total_tokens = _add(row.total_tokens, counters.total_tokens)
        if (
            counters.input_tokens is not None
            and counters.output_tokens is not None
            and counters.total_tokens != counters.input_tokens + counters.output_tokens
        ):
            row.contradictory_totals += 1
    elif counters.input_tokens is not None and counters.output_tokens is not None:
        row.derived_totals += 1
        row.total_tokens = _add(
            row.total_tokens, counters.input_tokens + counters.output_tokens
        )


def aggregate_usage(
    snapshot: ReportingSnapshot,
    *,
    group_by: Sequence[GroupDimension] = DEFAULT_GROUP_BY,
) -> AnalyticsReport:
    """Aggregate canonical attempts once per requested dimension combination."""

    allowed = set(DEFAULT_GROUP_BY)
    selected = tuple(group_by or DEFAULT_GROUP_BY)
    unknown = [item for item in selected if item not in allowed]
    if unknown:
        raise ValueError(f"unsupported grouping dimension: {unknown[0]}")
    if not snapshot.available:
        return AnalyticsReport(
            schema_version=1,
            feature_id=snapshot.feature_id,
            group_by=selected,
            available=False,
            limit_reason=snapshot.limit_reason,
            legacy_charge=snapshot.legacy_charge,
            budget_charge=snapshot.budget_charge,
            spent=snapshot.spent,
        )
    rows: dict[tuple, AggregateRow] = {}
    totals = AggregateRow(dimensions={"all": _NULL})
    for record in snapshot.attempts:
        if record.legacy or record.stage == PROBE_STAGE or record.feature_id == PROBE_FEATURE_ID:
            continue
        _add_record(_row_for(rows, record, selected, snapshot.case_id), record)
        _add_record(totals, record)
    ordered = sorted(
        rows.values(),
        key=lambda item: tuple((item.dimensions.get(name) or "") for name in selected),
    )
    totals.legacy_charge = snapshot.legacy_charge
    return AnalyticsReport(
        schema_version=1,
        feature_id=snapshot.feature_id,
        group_by=selected,
        rows=ordered,
        totals=totals,
        legacy_charge=snapshot.legacy_charge,
        budget_charge=snapshot.budget_charge,
        spent=snapshot.spent,
        measured_charge=snapshot.measured_charge,
        estimated_charge=snapshot.estimated_charge,
        unresolved_charge=snapshot.unresolved_charge,
        available=True,
        presence=snapshot.presence.model_dump(),
    )


def aggregate_many(
    snapshots: Iterable[ReportingSnapshot],
    *,
    group_by: Sequence[GroupDimension] = ("feature", "stage", "model", "strategy"),
) -> AnalyticsReport:
    """Aggregate several feature snapshots for checkout-wide reporting."""

    selected = tuple(group_by or DEFAULT_GROUP_BY)
    combined = ReportingSnapshot(
        feature_id="__all__",
        available=True,
        attempts=[],
        legacy_charge=0,
        budget_charge=0,
        measured_charge=0,
        estimated_charge=0,
        unresolved_charge=0,
        spent=0,
    )
    attempted = 0
    for snapshot in snapshots:
        if not snapshot.available:
            continue
        attempted += 1
        combined.attempts.extend(snapshot.attempts)
        combined.legacy_charge += snapshot.legacy_charge
        combined.budget_charge += snapshot.budget_charge
        combined.measured_charge += snapshot.measured_charge
        combined.estimated_charge += snapshot.estimated_charge
        combined.unresolved_charge += snapshot.unresolved_charge
        combined.spent += snapshot.spent
    if attempted == 0:
        combined.available = False
        combined.limit_reason = "no_available_journals"
    report = aggregate_usage(combined, group_by=selected)
    if report.totals is not None:
        report.totals.legacy_charge = combined.legacy_charge
    return report
