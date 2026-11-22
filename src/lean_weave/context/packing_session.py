"""Opaque, process-local task state for bounded L2 context expansion.

A session never stores source bodies, prompts, tool replay or a live scanner.
It stores revisions, coverage intervals, fragment references and delivery
receipts so a later expansion can prove freshness before resending anything.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Mapping

from lean_weave.config.models import PackingConfig
from lean_weave.context.packing_models import (
    CandidateRef,
    ExpansionDelivery,
    FragmentRef,
    RelationEvidence,
)
from lean_weave.context.repository import FrozenAdmissionPolicy

Interval = tuple[int, int]


@dataclass
class TaskProjection:
    """Immutable task settings fixed when the session opened."""

    task_id: str
    query: str
    task_specification: str
    system_instructions: str
    level: str
    output_paths: tuple[str, ...] | None


class PackingSession:
    """Single-owner session; never serialized or shared across threads."""

    __slots__ = (
        "session_id",
        "task",
        "config",
        "model",
        "thinking",
        "output_tokens",
        "root_binding",
        "policy",
        "revisions",
        "fragments",
        "coverage",
        "candidates",
        "relations",
        "cumulative_units",
        "attempts",
        "max_expansions",
        "receipts",
        "closed",
        "file_chars",
        "total_chars",
    )

    def __init__(
        self,
        *,
        task: TaskProjection,
        config: PackingConfig,
        model: str,
        thinking: str,
        output_tokens: int,
        root_binding: tuple[str, int, int],
        policy: FrozenAdmissionPolicy,
        revisions: Mapping[str, str],
        fragments: Mapping[str, FragmentRef],
        coverage: Mapping[str, tuple[Interval, ...]],
        candidates: tuple[CandidateRef, ...],
        relations: tuple[RelationEvidence, ...],
        cumulative_units: int,
        file_chars: Mapping[str, int] | None = None,
    ) -> None:
        self.session_id = "pack-session:" + uuid.uuid4().hex
        self.task = task
        self.config = config
        self.model = model
        self.thinking = thinking
        self.output_tokens = output_tokens
        self.root_binding = root_binding
        self.policy = policy
        self.revisions = dict(revisions)
        self.fragments = dict(fragments)
        self.coverage = {key: tuple(value) for key, value in coverage.items()}
        self.candidates = tuple(candidates)
        self.relations = tuple(relations)
        self.cumulative_units = int(cumulative_units)
        self.attempts = 0
        self.max_expansions = config.max_expansions
        self.receipts: dict[str, ExpansionDelivery] = {}
        self.closed = False
        self.file_chars = dict(file_chars or {})
        self.total_chars = sum(self.file_chars.values())

    @property
    def open(self) -> bool:
        return not self.closed

    @property
    def remaining_expansions(self) -> int:
        return max(0, self.max_expansions - self.attempts)

    def close(self) -> None:
        self.closed = True
        self.fragments.clear()
        self.coverage.clear()
        self.candidates = ()
        self.relations = ()
        self.receipts.clear()

    # -- coverage bookkeeping ----------------------------------------
    def coverage_key(self, path: str, digest: str) -> str:
        return f"{path}@{digest}"

    def covered_intervals(self, path: str, digest: str) -> tuple[Interval, ...]:
        return self.coverage.get(self.coverage_key(path, digest), ())

    def merged_coverage(self, path: str, digest: str) -> tuple[Interval, ...]:
        return _merge(self.covered_intervals(path, digest))

    def add_coverage(
        self, path: str, digest: str, start: int, end: int
    ) -> tuple[Interval, ...]:
        key = self.coverage_key(path, digest)
        merged = _merge((*self.coverage.get(key, ()), (start, end)))
        self.coverage[key] = merged
        return merged

    def record_fragment(self, ref: FragmentRef) -> None:
        self.fragments[ref.fragment_id] = ref

    def record_receipt(self, receipt: ExpansionDelivery) -> None:
        self.receipts[receipt.receipt_id] = receipt

    def has_receipt(self, receipt_id: str) -> bool:
        return receipt_id in self.receipts


def _merge(intervals: tuple[Interval, ...]) -> tuple[Interval, ...]:
    ordered = sorted(intervals)
    merged: list[Interval] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return tuple(merged)
