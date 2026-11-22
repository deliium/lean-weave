"""Static import-relation codec and importer-scoped cache adapter."""

from __future__ import annotations

import json
from typing import Callable, Iterable

from lean_weave.code_index.models import FileRecord, facts_sha256
from lean_weave.context_cache.models import (
    ArtifactKind,
    CacheBatch,
    CacheCodec,
    CacheKey,
    CacheProof,
    ContextCache,
)
from lean_weave.context_cache.service import CachePolicy
from lean_weave.context.policy import contains_secret
from lean_weave.repository_map.graph import (
    consulted_keys_for_import,
    resolve_imports_for,
)
from lean_weave.repository_map.models import (
    GRAPH_CONTRACT_VERSION,
    ImportRelation,
    ResolutionCandidate,
    SourceRef,
)
from lean_weave.errors import CodeIndexError
from lean_weave.logging_setup import get_logger

log = get_logger("context_cache.imports")

IMPORT_CODEC_VERSION = 1
_STATUSES = frozenset({"resolved", "ambiguous", "unresolved"})
_REASONS = frozenset(
    {
        None,
        "ambiguous_parent",
        "non_package_parent",
        "ambiguous_module",
        "not_indexed",
        "ambiguous_member",
        "unresolved_member",
        "invalid_relative",
        "wildcard_module_only",
        "ambiguous_child_or_member",
    }
)


def _is_package(path: str) -> bool:
    return path.endswith("/__init__.py") or path.endswith("/__init__.pyi") or path in {
        "__init__.py",
        "__init__.pyi",
    }


def _safe_name(value: str) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        and not contains_secret(value)
    )


def _watched_payload(watched: Iterable[str], catalogs) -> list[object]:
    payload: list[object] = []
    for key in sorted(set(watched)):
        candidates: list[object] = []
        for file_id in catalogs.by_module.get(key, ()):
            node = catalogs.nodes[file_id]
            record = catalogs.records[file_id]
            definitions = sorted(
                (
                    name,
                    symbol_id,
                )
                for (owner, name), ids in catalogs.top_level.items()
                if owner == file_id
                for symbol_id in ids
            )
            candidates.append(
                [
                    node.file_id,
                    node.path,
                    node.module_name,
                    node.digest,
                    facts_sha256(record.facts),
                    _is_package(node.path),
                    [[name, symbol_id] for name, symbol_id in definitions],
                ]
            )
        payload.append([key, candidates])
    return payload


class ImportCodec(CacheCodec[tuple]):
    kind = ArtifactKind.IMPORTS

    def __init__(
        self,
        *,
        record: FileRecord,
        node,
        catalogs,
        watched: frozenset[str],
    ) -> None:
        self.record = record
        self.node = node
        self.catalogs = catalogs
        self.watched = watched

    def encode(self, value) -> bytes:
        relations, watched = value
        return json.dumps(
            {
                "relations": [_relation_payload(item) for item in relations],
                "watched": sorted(watched),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

    def decode(self, payload: bytes):
        raw = json.loads(payload.decode("utf-8"))
        if not isinstance(raw, dict) or set(raw) != {"relations", "watched"}:
            raise ValueError("imports")
        relations = tuple(_relation_from_payload(item) for item in raw["relations"])
        watched = frozenset(str(item) for item in raw["watched"])
        return relations, watched

    def validate(self, value, proof: CacheProof) -> bool:
        relations, watched = value
        if watched != self.watched:
            return False
        if not isinstance(relations, tuple):
            return False
        for item in relations:
            if not _valid_relation(item, self.record, self.catalogs):
                return False
        return True


def _relation_payload(item: ImportRelation) -> dict:
    return {
        "importer_id": item.importer_id,
        "path": item.source.path,
        "digest": item.source.digest,
        "start": item.source.start_line,
        "end": item.source.end_line,
        "statement": item.statement_ordinal,
        "name": item.name_ordinal,
        "spelling": item.spelling,
        "alias": item.alias,
        "status": item.status,
        "reason": item.reason,
        "target_id": item.target_id,
        "symbol_id": item.symbol_id,
        "candidates": [
            [candidate.file_id, candidate.path, candidate.module_name]
            for candidate in item.candidates
        ],
    }


def _relation_from_payload(value: object) -> ImportRelation:
    if not isinstance(value, dict):
        raise ValueError("relation")
    path = str(value["path"])
    digest = str(value["digest"])
    start = value["start"]
    end = value["end"]
    if start is not None and (type(start) is not int or start < 1):
        raise ValueError("start")
    if end is not None and (type(end) is not int or end < 1):
        raise ValueError("end")
    candidates = tuple(
        ResolutionCandidate(
            file_id=str(item[0]), path=str(item[1]), module_name=str(item[2])
        )
        for item in value["candidates"]
        if isinstance(item, list) and len(item) == 3
    )
    return ImportRelation(
        importer_id=str(value["importer_id"]),
        source=SourceRef(path=path, digest=digest, start_line=start, end_line=end),
        statement_ordinal=int(value["statement"]),
        name_ordinal=int(value["name"]),
        spelling=str(value["spelling"]),
        alias=None if value["alias"] is None else str(value["alias"]),
        status=str(value["status"]),
        reason=None if value["reason"] is None else str(value["reason"]),
        target_id=None if value["target_id"] is None else str(value["target_id"]),
        symbol_id=None if value["symbol_id"] is None else str(value["symbol_id"]),
        candidates=candidates,
    )


def _valid_relation(item: ImportRelation, record: FileRecord, catalogs) -> bool:
    if item.importer_id != record.file_id:
        return False
    if item.source.path != record.path or item.source.digest != record.digest:
        return False
    if item.source.start_line is None or item.source.end_line is None:
        return False
    if (
        item.source.start_line < 1
        or item.source.end_line < item.source.start_line
        or item.source.end_line > max(record.line_count, 1)
    ):
        return False
    if item.statement_ordinal < 0 or item.name_ordinal < 0:
        return False
    if item.status not in _STATUSES or item.reason not in _REASONS:
        return False
    if not _safe_name(item.spelling):
        return False
    if item.alias is not None and not _safe_name(item.alias):
        return False
    if item.target_id is not None and item.target_id not in catalogs.nodes:
        return False
    if item.symbol_id is not None and item.symbol_id not in catalogs.symbols:
        return False
    for candidate in item.candidates:
        if candidate.file_id not in catalogs.nodes:
            return False
        if catalogs.nodes[candidate.file_id].path != candidate.path:
            return False
        if catalogs.nodes[candidate.file_id].module_name != candidate.module_name:
            return False
    return True


def _watched_keys(record: FileRecord, node) -> frozenset[str]:
    keys: set[str] = set()
    for fact in record.facts.imports:
        keys.update(
            consulted_keys_for_import(
                kind=fact.kind,
                module=fact.module,
                level=fact.level,
                names=fact.names,
                node=node,
            )
        )
    return frozenset(keys)


class ImportCache:
    """Importer-scoped relation reuse over one checked generation."""

    def __init__(self, cache: ContextCache, *, policy: CachePolicy, header=None) -> None:
        self.cache = cache
        self.policy = policy
        self.header = header

    def bind(self, header) -> "ImportCache":
        self.header = header
        return self

    def key(self, record: FileRecord, node) -> CacheKey:
        return CacheKey.create(
            ArtifactKind.IMPORTS,
            algorithm={
                "codec": IMPORT_CODEC_VERSION,
                "graph": GRAPH_CONTRACT_VERSION,
            },
            config={
                "policy": self.policy.digest(),
                "extractor": (
                    self.header.extractor_key if self.header is not None else ""
                ),
                "schema": (
                    self.header.schema_version if self.header is not None else 0
                ),
            },
            inputs={"importer": record.path},
        )

    def proof(self, record: FileRecord, node, catalogs, watched: frozenset[str]) -> CacheProof:
        return CacheProof.create(
            ArtifactKind.IMPORTS,
            inputs={
                "importer": [
                    record.file_id,
                    record.path,
                    record.digest,
                    facts_sha256(record.facts),
                ],
                "watched": _watched_payload(watched, catalogs),
            },
        )

    def resolve(
        self,
        record: FileRecord,
        node,
        catalogs,
        *,
        batch: CacheBatch | None,
        compute: Callable[[], tuple[tuple[ImportRelation, ...], frozenset[str]]] | None = None,
    ):
        watched = _watched_keys(record, node)
        key = self.key(record, node)
        proof = self.proof(record, node, catalogs, watched)
        codec = ImportCodec(
            record=record, node=node, catalogs=catalogs, watched=watched
        )
        outcome = self.cache.lookup(key, proof, codec=codec)
        if outcome.hit:
            return outcome.value
        try:
            value = compute() if compute is not None else resolve_imports_for(
                node, record, catalogs
            )
        except Exception:
            return None
        if batch is not None:
            self.cache.publish(key, proof, value, codec=codec, batch=batch)
        return value
