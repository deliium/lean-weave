"""Bounded, non-persistent workflow context telemetry.

Events contain only counts, stable categories, opaque ordinal IDs, and local
estimates.  Repository paths, source bodies, cache keys, prompts, diagnostics,
tool arguments, and reasoning are never recorded.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from lean_weave.logging_setup import get_logger

log = get_logger("workflow.context_metrics")

MAX_ORDINALS = 128
MAX_SECTION_NAMES = 8


@dataclass(frozen=True)
class FragmentOrdinal:
    """Opaque per-attempt fragment identity plus its coarse category."""

    ordinal: int
    kind: str


@dataclass(frozen=True)
class ContextStageEvent:
    stage: str
    mode: str
    outcome: str
    fragments: tuple[FragmentOrdinal, ...] = ()
    selection_estimates: tuple[tuple[str, int], ...] = ()
    wire_input_estimate: int = 0
    retrieval_ms: int = 0
    expansion_attempts: int = 0
    expansion_successes: int = 0
    duplicate_fragments: int = 0
    duplicate_bytes: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    cache_observations_available: bool = True
    refused: bool = False
    refusal_category: str | None = None


class ContextObserver(Protocol):
    def observe(self, event: ContextStageEvent) -> None: ...


@dataclass
class RecordingContextObserver:
    """Test/benchmark collector; never serialized into FeatureState."""

    events: list[ContextStageEvent] = field(default_factory=list)

    def observe(self, event: ContextStageEvent) -> None:
        self.events.append(event)


@dataclass
class CacheOperationCounts:
    hits: int = 0
    misses: int = 0
    available: bool = True

    def observe(self, kind: str, result: str, reason: str | None = None) -> None:
        # Only the stable result category matters; kind and reason are not
        # stored here, so no cache key or path can leak.
        del kind, reason
        if result == "hit":
            self.hits += 1
        elif result in {"miss", "invalidated", "bypass"}:
            self.misses += 1


@dataclass
class _AttemptTelemetry:
    """Mutable per-attempt bookkeeping; converted to an immutable event."""

    stage: str = ""
    mode: str = "v1"
    retrieval_ms: int = 0
    expansion_attempts: int = 0
    expansion_successes: int = 0
    duplicate_fragments: int = 0
    duplicate_bytes: int = 0
    fragments: list[FragmentOrdinal] = field(default_factory=list)
    estimates: dict[str, int] = field(default_factory=dict)
    wire_input_estimate: int = 0
    outcome: str = "pending"
    refused: bool = False
    refusal_category: str | None = None
    cache: CacheOperationCounts = field(default_factory=CacheOperationCounts)
    _ordinals: dict[str, int] = field(default_factory=dict)

    def record_fragment(self, identity: str, kind: str) -> FragmentOrdinal:
        existing = self._ordinals.get(identity)
        if existing is not None:
            return FragmentOrdinal(existing, kind)
        ordinal = len(self._ordinals)
        self._ordinals[identity] = ordinal
        if len(self.fragments) < MAX_ORDINALS:
            self.fragments.append(FragmentOrdinal(ordinal, kind))
        return FragmentOrdinal(ordinal, kind)

    def elapsed(self, started: float) -> None:
        self.retrieval_ms += max(0, int((time.monotonic() - started) * 1000))

    def freeze(self) -> ContextStageEvent:
        return ContextStageEvent(
            stage=self.stage,
            mode=self.mode,
            outcome=self.outcome,
            fragments=tuple(self.fragments),
            selection_estimates=tuple(
                sorted(self.estimates.items())[:MAX_SECTION_NAMES]
            ),
            wire_input_estimate=max(0, self.wire_input_estimate),
            retrieval_ms=max(0, self.retrieval_ms),
            expansion_attempts=max(0, self.expansion_attempts),
            expansion_successes=max(0, self.expansion_successes),
            duplicate_fragments=max(0, self.duplicate_fragments),
            duplicate_bytes=max(0, self.duplicate_bytes),
            cache_hits=max(0, self.cache.hits),
            cache_misses=max(0, self.cache.misses),
            cache_observations_available=self.cache.available,
            refused=self.refused,
            refusal_category=self.refusal_category,
        )


def emit(observer: ContextObserver | None, event: ContextStageEvent) -> None:
    """Deliver one event; observation failure never changes workflow outcome."""

    log.debug(
        "[Workflow.context] stage=%s mode=%s outcome=%s fragments=%s wire_input=%s "
        "retrieval_ms=%s expansions=%s/%s duplicates=%s/%s cache=%s/%s",
        event.stage,
        event.mode,
        event.outcome,
        len(event.fragments),
        event.wire_input_estimate,
        event.retrieval_ms,
        event.expansion_successes,
        event.expansion_attempts,
        event.duplicate_bytes,
        event.duplicate_fragments,
        event.cache_hits,
        event.cache_misses,
    )
    if observer is None:
        return
    try:
        observer.observe(event)
    except Exception:  # noqa: BLE001 - telemetry must not authorize or block work
        log.warning("[Workflow.context] outcome=observer_failed")


def safe_section_estimates(values: Iterable[tuple[str, int]]) -> dict[str, int]:
    allowed = {"system", "task", "map", "source", "tool", "wire"}
    result: dict[str, int] = {}
    for name, value in values:
        if name in allowed and isinstance(value, int) and not isinstance(value, bool):
            result[name] = max(0, int(value))
    return result
