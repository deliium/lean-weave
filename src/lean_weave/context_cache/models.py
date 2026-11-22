"""Versioned, LLM-free contracts for the disposable context cache.

The cache stores bounded deterministic metadata only.  It never owns source
authority, prompt material, task text, model output, or workflow state.  Keys
and proofs are built by typed adapters from freshly admitted inputs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, Mapping, Protocol, TypeVar, runtime_checkable

CACHE_CONTRACT_VERSION = 1
KEY_DOMAIN = "lw-context-key-v1"
PROOF_DOMAIN = "lw-context-proof-v1"
ENVELOPE_DOMAIN = "lw-context-envelope-v1"
PAYLOAD_BYTES_MAX = 256 * 1024
ROW_OVERHEAD_BYTES = 256

T = TypeVar("T")


class ArtifactKind(str, Enum):
    """The six independently reusable deterministic artifact kinds."""

    FACTS = "facts"
    IMPORTS = "imports"
    MAP_ROWS = "map_rows"
    RETRIEVAL_CANDIDATES = "retrieval_candidates"
    RANKING_RESULT = "ranking_result"
    PACKING_SPANS = "packing_spans"


class CacheResult(str, Enum):
    HIT = "hit"
    MISS = "miss"
    INVALIDATED = "invalidated"
    BYPASS = "bypass"


class CacheReason(str, Enum):
    """Bounded reasons; never carry user input, paths, or SQL text."""

    ABSENT = "absent"
    STALE_PROOF = "stale_proof"
    UNKNOWN_KIND = "unknown_kind"
    MALFORMED_ROW = "malformed_row"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    OVERSIZE = "oversize"
    UNAVAILABLE = "unavailable"
    UNSAFE = "unsafe"
    BUSY = "busy"
    CORRUPT = "corrupt"
    INCOMPATIBLE = "incompatible"
    CAPACITY = "capacity"
    EPOCH_CHANGED = "epoch_changed"
    DISABLED = "disabled"
    INVALID_INPUT = "invalid_input"
    RECOVERY_REFUSED = "recovery_refused"


class CacheHealth(str, Enum):
    DISABLED = "disabled"
    MISSING = "missing"
    READY = "ready"
    BUSY = "busy"
    CORRUPT = "corrupt"
    INCOMPATIBLE = "incompatible"
    UNSAFE = "unsafe"
    UNAVAILABLE = "unavailable"
    CAPACITY = "capacity"


class CacheEvent(str, Enum):
    HIT = "hit"
    MISS = "miss"
    INVALIDATION = "invalidation"
    REBUILD = "rebuild"
    CORRUPT_ENTRY = "corrupt_entry"
    EVICTION = "eviction"
    BYPASS = "bypass"


def _canonical(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError):
        raise ValueError("cache metadata") from None


def _digest(domain: str, value: object) -> str:
    text = _canonical(value)
    return "sha256:" + hashlib.sha256(
        (domain + "\x00" + text).encode("utf-8")
    ).hexdigest()


def _bounded(value: object, *, depth: int = 0) -> object:
    """Return a copy of JSON primitives with a hard structural bound."""

    if depth > 12:
        raise ValueError("cache metadata depth")
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str) and len(value.encode("utf-8")) > 16_384:
            raise ValueError("cache metadata string")
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError("cache metadata number")
        return value
    if isinstance(value, Mapping):
        if len(value) > 4096:
            raise ValueError("cache metadata mapping")
        result: dict[str, object] = {}
        for key in sorted(value):
            if not isinstance(key, str):
                raise ValueError("cache metadata key")
            result[key] = _bounded(value[key], depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 8192:
            raise ValueError("cache metadata sequence")
        return [_bounded(item, depth=depth + 1) for item in value]
    raise ValueError("cache metadata type")


@dataclass(frozen=True)
class CacheKey:
    """Logical cache identity: kind plus a domain-separated digest."""

    kind: ArtifactKind
    digest: str = field(compare=True)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ArtifactKind):
            raise ValueError("cache kind")
        if not _is_digest(self.digest):
            raise ValueError("cache key digest")

    @classmethod
    def create(
        cls,
        kind: ArtifactKind,
        *,
        algorithm: Mapping[str, object] | None = None,
        config: Mapping[str, object] | None = None,
        inputs: Mapping[str, object] | object = None,
    ) -> "CacheKey":
        payload = [
            KEY_DOMAIN,
            CACHE_CONTRACT_VERSION,
            kind.value,
            _bounded(dict(algorithm or {})),
            _bounded(dict(config or {})),
            _bounded(inputs),
        ]
        return cls(kind, _digest(KEY_DOMAIN, payload))


@dataclass(frozen=True)
class CacheProof:
    """Live admission witness for one logical key."""

    kind: ArtifactKind
    digest: str = field(compare=True)
    source_revision: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ArtifactKind):
            raise ValueError("cache kind")
        if not _is_digest(self.digest):
            raise ValueError("cache proof digest")
        if self.source_revision is not None and not _is_digest(self.source_revision):
            raise ValueError("cache revision")

    @classmethod
    def create(
        cls,
        kind: ArtifactKind,
        *,
        source_revision: str | None = None,
        inputs: object = None,
    ) -> "CacheProof":
        payload = [
            PROOF_DOMAIN,
            CACHE_CONTRACT_VERSION,
            kind.value,
            source_revision,
            _bounded(inputs),
        ]
        return cls(kind, _digest(PROOF_DOMAIN, payload), source_revision)


@dataclass(frozen=True)
class CacheOutcome(Generic[T]):
    result: CacheResult
    value: T | None = None
    reason: CacheReason | None = None

    @property
    def hit(self) -> bool:
        return self.result is CacheResult.HIT and self.value is not None


@dataclass(frozen=True)
class CacheStatus:
    enabled: bool
    health: CacheHealth
    schema_version: int | None = None
    entries: int | None = None
    db_bytes: int | None = None
    max_entries: int = 0
    max_bytes: int = 0
    migration_pending: bool = False
    kind_counts: tuple[tuple[str, int], ...] = ()

    @property
    def usable(self) -> bool:
        return self.enabled and self.health is CacheHealth.READY


@dataclass(frozen=True)
class CacheStats:
    enabled: bool
    health: CacheHealth
    events: tuple[tuple[str, int], ...] = ()
    kind_events: tuple[tuple[str, tuple[tuple[str, int], ...]], ...] = ()

    def count(self, event: CacheEvent | str, kind: ArtifactKind | str | None = None) -> int:
        event_name = event.value if isinstance(event, CacheEvent) else str(event)
        if kind is None:
            return dict(self.events).get(event_name, 0)
        kind_name = kind.value if isinstance(kind, ArtifactKind) else str(kind)
        return dict(dict(self.kind_events).get(kind_name, ())).get(event_name, 0)


@runtime_checkable
class CacheCodec(Protocol[T]):
    """Canonical serialization plus shape and live-domain validation."""

    kind: ArtifactKind

    def encode(self, value: T) -> bytes:
        ...

    def decode(self, payload: bytes) -> T:
        ...

    def validate(self, value: T, proof: CacheProof) -> bool:
        ...


@runtime_checkable
class ContextCache(Protocol):
    """Versioned cache surface. Construction performs no I/O."""

    @property
    def enabled(self) -> bool:
        ...

    def lookup(self, key: CacheKey, proof: CacheProof, *, codec: CacheCodec[T]) -> CacheOutcome[T]:
        ...

    def publish(
        self,
        key: CacheKey,
        proof: CacheProof,
        value: T,
        *,
        codec: CacheCodec[T],
        batch: "CacheBatch | None" = None,
    ) -> bool:
        ...

    def begin_batch(self) -> "CacheBatch":
        ...

    def status(self) -> CacheStatus:
        ...

    def stats(self) -> CacheStats:
        ...

    def clear(self) -> bool:
        ...

    def rebuild(self, seed, *, revalidate) -> CacheStatus:
        ...


class CacheBatch:
    """Operation-local staging and epoch fence."""

    def __init__(self, epoch: bytes | None, *, usable: bool = True) -> None:
        self.epoch = epoch
        self.usable = usable
        self.rows: list[tuple[CacheKey, CacheProof, bytes, ArtifactKind]] = []
        self.events: list[tuple[CacheEvent, ArtifactKind]] = []
        self.bytes = 0
        self.closed = False

    def stage(
        self,
        key: CacheKey,
        proof: CacheProof,
        payload: bytes,
        *,
        kind: ArtifactKind,
    ) -> bool:
        if self.closed or not self.usable:
            return False
        if len(payload) > PAYLOAD_BYTES_MAX:
            return False
        if len(self.rows) >= 16_384 or self.bytes + len(payload) > 64 * 1024 * 1024:
            return False
        self.rows.append((key, proof, payload, kind))
        self.bytes += len(payload) + ROW_OVERHEAD_BYTES
        return True

    def record(self, event: CacheEvent, kind: ArtifactKind) -> None:
        if not self.closed:
            self.events.append((event, kind))

    def discard(self) -> None:
        self.closed = True
        self.usable = False
        self.rows.clear()
        self.events.clear()
        self.bytes = 0


class DisabledContextCache:
    """A cache that never resolves roots, creates files, or opens a database."""

    enabled = False

    def lookup(self, key: CacheKey, proof: CacheProof, *, codec: CacheCodec[T]) -> CacheOutcome[T]:
        return CacheOutcome(CacheResult.BYPASS, None, CacheReason.DISABLED)

    def publish(self, key, proof, value, *, codec, batch=None) -> bool:  # noqa: ANN001
        return False

    def begin_batch(self) -> CacheBatch:
        return CacheBatch(None, usable=False)

    def status(self) -> CacheStatus:
        return CacheStatus(False, CacheHealth.DISABLED)

    def stats(self) -> CacheStats:
        return CacheStats(False, CacheHealth.DISABLED)

    def clear(self) -> bool:
        return False

    def rebuild(self, seed, *, revalidate) -> CacheStatus:  # noqa: ANN001
        return self.status()


def envelope_checksum(
    *,
    checkout_key: str,
    kind: ArtifactKind,
    key_digest: str,
    proof_digest: str,
    source_revision: str | None,
    payload: bytes,
) -> str:
    """Checksum accidental corruption; not hostile same-UID authenticity."""

    hasher = hashlib.sha256()
    hasher.update(ENVELOPE_DOMAIN.encode("ascii"))
    hasher.update(b"\x00")
    for part in (
        checkout_key,
        kind.value,
        key_digest,
        proof_digest,
        source_revision or "",
    ):
        encoded = part.encode("utf-8")
        hasher.update(len(encoded).to_bytes(4, "big"))
        hasher.update(encoded)
    hasher.update(len(payload).to_bytes(8, "big"))
    hasher.update(payload)
    return "sha256:" + hasher.hexdigest()


def _is_digest(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    body = value[len("sha256:"):]
    return len(body) == 64 and all(char in "0123456789abcdef" for char in body)
