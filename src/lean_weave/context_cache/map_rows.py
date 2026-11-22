"""Independently reusable repository-map row fragments."""

from __future__ import annotations

import json

from lean_weave.context.policy import contains_secret
from lean_weave.context_cache.models import (
    ArtifactKind,
    CacheBatch,
    CacheCodec,
    CacheKey,
    CacheProof,
    ContextCache,
)
from lean_weave.context_cache.service import CachePolicy
from lean_weave.repository_map import render
from lean_weave.repository_map.models import MapSymbol, ModuleNode

MAP_ROW_CODEC_VERSION = 1
_ROW_BYTES_MAX = 16_384


def _safe_row(value: str | None) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    if len(value.encode("utf-8")) > _ROW_BYTES_MAX:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    return not contains_secret(value)


class HeaderCodec(CacheCodec[dict]):
    kind = ArtifactKind.MAP_ROWS

    def __init__(self, module_name: str, path: str) -> None:
        self.module_name = module_name
        self.path = path

    def encode(self, value: dict) -> bytes:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")

    def decode(self, payload: bytes) -> dict:
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict) or set(value) != {"row", "module", "path"}:
            raise ValueError("header row")
        return value

    def validate(self, value: dict, proof: CacheProof) -> bool:
        return (
            value.get("module") == self.module_name
            and value.get("path") == self.path
            and _safe_row(value.get("row"))
        )


class DefinitionCodec(CacheCodec[dict]):
    kind = ArtifactKind.MAP_ROWS

    def __init__(self, symbol: MapSymbol) -> None:
        self.symbol = symbol

    def encode(self, value: dict) -> bytes:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")

    def decode(self, payload: bytes) -> dict:
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict) or set(value) != {"long", "short", "too_big"}:
            raise ValueError("definition row")
        return value

    def validate(self, value: dict, proof: CacheProof) -> bool:
        return (
            isinstance(value.get("too_big"), bool)
            and _safe_row(value.get("long"))
            and _safe_row(value.get("short"))
        )


class MapRowCache:
    """Header and definition row fragments keyed without budgets or selectors."""

    def __init__(self, cache: ContextCache, *, policy: CachePolicy) -> None:
        self.cache = cache
        self.policy = policy

    def _header_key(self, module_name: str, path: str) -> CacheKey:
        return CacheKey.create(
            ArtifactKind.MAP_ROWS,
            algorithm={"codec": MAP_ROW_CODEC_VERSION, "variant": "header"},
            config={"policy": self.policy.digest()},
            inputs={"path": path},
        )

    def _header_proof(self, module_name: str, path: str, digest: str) -> CacheProof:
        return CacheProof.create(
            ArtifactKind.MAP_ROWS,
            inputs={
                "variant": "header",
                "path": path,
                "module": module_name,
                "digest": digest,
            },
        )

    def header(
        self,
        module_name: str,
        path: str,
        digest: str,
        *,
        batch: CacheBatch | None,
    ) -> str | None:
        codec = HeaderCodec(module_name, path)
        key = self._header_key(module_name, path)
        proof = self._header_proof(module_name, path, digest)
        outcome = self.cache.lookup(key, proof, codec=codec)
        if outcome.hit:
            return outcome.value.get("row")
        row = render.module_row(module_name, path)
        if batch is not None:
            self.cache.publish(
                key,
                proof,
                {"row": row, "module": module_name, "path": path},
                codec=codec,
                batch=batch,
            )
        return row

    def definition(
        self, symbol: MapSymbol, *, batch: CacheBatch | None
    ) -> tuple[str | None, str | None, bool]:
        codec = DefinitionCodec(symbol)
        key = CacheKey.create(
            ArtifactKind.MAP_ROWS,
            algorithm={"codec": MAP_ROW_CODEC_VERSION, "variant": "definition"},
            config={"policy": self.policy.digest()},
            inputs={
                "path": symbol.source.path,
                "qualified": symbol.qualified_name,
                "kind": symbol.kind,
                "occurrence": symbol.occurrence,
            },
        )
        proof = CacheProof.create(
            ArtifactKind.MAP_ROWS,
            inputs={
                "variant": "definition",
                "path": symbol.source.path,
                "digest": symbol.source.digest,
                "start": symbol.source.start_line,
                "end": symbol.source.end_line,
                "qualified": symbol.qualified_name,
                "kind": symbol.kind,
                "signature": symbol.signature,
            },
        )
        outcome = self.cache.lookup(key, proof, codec=codec)
        if outcome.hit:
            return outcome.value["long"], outcome.value["short"], bool(
                outcome.value["too_big"]
            )
        long = render.definition_row(
            symbol.source.path,
            symbol.source.start_line or 0,
            symbol.source.end_line or 0,
            symbol.qualified_name,
            symbol.signature,
            kind=symbol.kind,
        )
        short = render.definition_row(
            symbol.source.path,
            symbol.source.start_line or 0,
            symbol.source.end_line or 0,
            symbol.qualified_name,
            symbol.signature,
            kind=symbol.kind,
            short=True,
        )
        signature = symbol.signature or ""
        too_big = len(render.escape_map_field(signature).encode("utf-8")) > 160
        if batch is not None:
            self.cache.publish(
                key,
                proof,
                {"long": long, "short": short, "too_big": too_big},
                codec=codec,
                batch=batch,
            )
        return long, short, too_big
