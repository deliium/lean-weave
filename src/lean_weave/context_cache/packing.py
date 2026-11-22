"""Structural Python-span codec and packing cache adapter."""

from __future__ import annotations

import json

from lean_weave.context.packing_source import SourceSpan
from lean_weave.context.policy import contains_secret
from lean_weave.context.repository import FileSnapshot
from lean_weave.context_cache.models import (
    ArtifactKind,
    CacheBatch,
    CacheCodec,
    CacheKey,
    CacheProof,
    ContextCache,
)
from lean_weave.context_cache.service import CachePolicy
from lean_weave.logging_setup import get_logger

log = get_logger("context_cache.packing")

SPAN_CODEC_VERSION = 1
_KINDS = frozenset({"definition", "statement"})
_MAX_SPANS = 65_536


def _safe_symbol(value: str | None) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or not value:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    return not contains_secret(value)


class SpanCodec(CacheCodec[tuple[SourceSpan, ...]]):
    kind = ArtifactKind.PACKING_SPANS

    def __init__(self, snapshot: FileSnapshot) -> None:
        self.snapshot = snapshot

    def encode(self, value: tuple[SourceSpan, ...]) -> bytes:
        if len(value) > _MAX_SPANS:
            raise ValueError("spans")
        return json.dumps(
            [
                [item.start, item.end, item.kind, item.symbol, item.order]
                for item in value
            ],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

    def decode(self, payload: bytes) -> tuple[SourceSpan, ...]:
        raw = json.loads(payload.decode("utf-8"))
        if not isinstance(raw, list) or len(raw) > _MAX_SPANS:
            raise ValueError("spans")
        spans: list[SourceSpan] = []
        for item in raw:
            if not isinstance(item, list) or len(item) != 5:
                raise ValueError("span")
            start, end, kind, symbol, order = item
            if type(start) is not int or type(end) is not int or type(order) is not int:
                raise ValueError("span")
            spans.append(
                SourceSpan(
                    start=start,
                    end=end,
                    kind=str(kind),
                    symbol=None if symbol is None else str(symbol),
                    order=order,
                )
            )
        return tuple(spans)

    def validate(self, value: tuple[SourceSpan, ...], proof: CacheProof) -> bool:
        if not isinstance(value, tuple):
            return False
        lines = self.snapshot.lines
        previous: tuple[int, int, int] | None = None
        for item in value:
            if not isinstance(item, SourceSpan):
                return False
            if item.kind not in _KINDS:
                return False
            if not _safe_symbol(item.symbol):
                return False
            if (
                item.start < 1
                or item.end < item.start
                or item.end > max(lines, 1)
                or item.order < 0
            ):
                return False
            key = (item.start, item.end, item.order)
            if previous is not None and key < previous:
                return False
            previous = key
        return True


class PackingSpanCache:
    """Path-local structural span reuse independent of packing budgets."""

    def __init__(self, cache: ContextCache, *, policy: CachePolicy) -> None:
        self.cache = cache
        self.policy = policy

    def key(self, snapshot: FileSnapshot) -> CacheKey:
        return CacheKey.create(
            ArtifactKind.PACKING_SPANS,
            algorithm={"codec": SPAN_CODEC_VERSION, "parser": "py-ast-spans-v1"},
            config={"policy": self.policy.digest()},
            inputs={"path": snapshot.path},
        )

    def proof(self, snapshot: FileSnapshot) -> CacheProof:
        return CacheProof.create(
            ArtifactKind.PACKING_SPANS,
            inputs={"digest": snapshot.digest, "lines": snapshot.lines},
        )

    def load(
        self,
        snapshot: FileSnapshot,
        *,
        batch: CacheBatch | None,
        compute,
    ) -> tuple[SourceSpan, ...]:
        codec = SpanCodec(snapshot)
        key = self.key(snapshot)
        proof = self.proof(snapshot)
        outcome = self.cache.lookup(key, proof, codec=codec)
        if outcome.hit:
            return outcome.value
        spans = compute()
        if batch is not None:
            self.cache.publish(key, proof, spans, codec=codec, batch=batch)
        return spans
