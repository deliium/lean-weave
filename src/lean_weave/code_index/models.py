"""Typed persistent-index contracts and canonical serialization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Mapping

from lean_weave.context.models import SymbolKind
from lean_weave.context.python_index import (
    DefinitionFact,
    ImportFact,
    PythonFacts,
    extractor_key,
    module_name_for_path,
)

INDEX_SCHEMA_VERSION = 1
POLICY_VERSION = 1
APPLICATION_ID = 0x4C574958  # "LWIX"
MAX_INDEX_BYTES = 64 * 1024 * 1024
MODULE_KIND = "module"


class StoreHealth(str, Enum):
    MISSING = "missing"
    HEALTHY = "healthy"
    INCOMPATIBLE = "incompatible"
    CORRUPT = "corrupt"
    UNAVAILABLE = "unavailable"


class Freshness(str, Enum):
    CURRENT = "current"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class IndexUpdate:
    revision: str
    schema_version: int
    counts: Mapping[str, int] = field(default_factory=dict)

    def count(self, name: str) -> int:
        return int(self.counts.get(name, 0))


@dataclass(frozen=True)
class IndexStatus:
    health: StoreHealth
    freshness: Freshness
    revision: str | None = None
    schema_version: int | None = None
    files: int | None = None
    symbols: int | None = None
    imports: int | None = None


@dataclass(frozen=True)
class IndexedSymbol:
    file_id: str
    symbol_id: str
    path: str
    digest: str
    revision: str
    module: str
    name: str
    simple_name: str
    kind: str
    parent: str | None
    signature: str | None
    decorators: tuple[str, ...]
    start_line: int | None
    end_line: int | None
    parse_status: str


@dataclass(frozen=True)
class SymbolLookup:
    revision: str
    hits: tuple[IndexedSymbol, ...]
    truncated: bool


@dataclass(frozen=True)
class IndexHeader:
    schema_version: int
    checkout_key: str
    revision: str
    policy_key: str
    extractor_key: str
    manifest_sha256: str
    files: int = 0
    symbols: int = 0
    imports: int = 0


@dataclass(frozen=True)
class FileRecord:
    path: str
    file_id: str
    module_name: str
    digest: str
    size_bytes: int
    line_count: int
    parse_status: str
    facts: PythonFacts


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_revision(policy_key: str, path_digests: Mapping[str, str] | Iterable[tuple[str, str]]) -> str:
    pairs = sorted(path_digests.items() if isinstance(path_digests, Mapping) else path_digests)
    return _sha256_text(_canonical_json(["lw-index-source-v1", policy_key, pairs]))


def policy_key(
    *,
    state_path: str,
    protected_paths: Iterable[str],
    exclusions: Iterable[str],
    candidate_bytes_max: int,
) -> str:
    payload = [
        "lw-index-policy-v1",
        POLICY_VERSION,
        state_path,
        sorted(protected_paths),
        list(exclusions),
        int(candidate_bytes_max),
    ]
    return _sha256_text(_canonical_json(payload))


def facts_payload(facts: PythonFacts) -> dict:
    return {
        "status": facts.status,
        "line_count": facts.line_count,
        "definitions": [
            {
                "local_name": item.local_name,
                "simple_name": item.simple_name,
                "kind": item.kind.value,
                "parent": item.parent,
                "signature": item.signature,
                "decorators": list(item.decorators),
                "start_line": item.start_line,
                "end_line": item.end_line,
                "traversal_order": item.traversal_order,
                "occurrence": item.occurrence,
            }
            for item in facts.definitions
        ],
        "imports": [
            {
                "kind": item.kind,
                "module": item.module,
                "level": item.level,
                "names": [[name, alias] for name, alias in item.names],
                "start_line": item.start_line,
                "end_line": item.end_line,
            }
            for item in facts.imports
        ],
    }


def facts_from_payload(payload: object) -> PythonFacts:
    if not isinstance(payload, dict):
        raise ValueError("facts payload")
    status = payload.get("status")
    if status not in {"ok", "syntax", "parse_limit"}:
        raise ValueError("facts status")
    line_count = payload.get("line_count")
    if isinstance(line_count, bool) or not isinstance(line_count, int) or line_count < 0:
        raise ValueError("facts line_count")
    definitions_payload = payload.get("definitions")
    imports_payload = payload.get("imports")
    if not isinstance(definitions_payload, list) or not isinstance(imports_payload, list):
        raise ValueError("facts shape")
    definitions: list[DefinitionFact] = []
    for item in definitions_payload:
        if not isinstance(item, dict):
            raise ValueError("definition")
        kind = SymbolKind(item["kind"])
        definitions.append(
            DefinitionFact(
                local_name=str(item["local_name"]),
                simple_name=str(item["simple_name"]),
                kind=kind,
                parent=None if item.get("parent") is None else str(item["parent"]),
                signature=str(item["signature"]),
                decorators=tuple(str(value) for value in item["decorators"]),
                start_line=int(item["start_line"]),
                end_line=int(item["end_line"]),
                traversal_order=int(item["traversal_order"]),
                occurrence=int(item["occurrence"]),
            )
        )
    imports: list[ImportFact] = []
    for item in imports_payload:
        if not isinstance(item, dict):
            raise ValueError("import")
        names = tuple((str(name), None if alias is None else str(alias)) for name, alias in item["names"])
        imports.append(
            ImportFact(
                kind=str(item["kind"]),
                module=None if item.get("module") is None else str(item["module"]),
                level=int(item["level"]),
                names=names,
                start_line=int(item["start_line"]),
                end_line=int(item["end_line"]),
            )
        )
    return PythonFacts(
        status=status,
        definitions=tuple(definitions),
        imports=tuple(imports),
        line_count=line_count,
    )


def facts_json(facts: PythonFacts) -> str:
    return _canonical_json(facts_payload(facts))


def facts_sha256(facts: PythonFacts) -> str:
    return _sha256_text(facts_json(facts))


def manifest_sha256(
    *,
    schema_version: int,
    checkout_key: str,
    revision: str,
    policy_key_value: str,
    extractor_key_value: str,
    files: Iterable[FileRecord],
) -> str:
    ordered = sorted(files, key=lambda item: item.path)
    payload = [
        "lw-index-manifest-v1",
        schema_version,
        checkout_key,
        revision,
        policy_key_value,
        extractor_key_value,
        [
            [item.path, item.file_id, item.digest, item.parse_status, facts_sha256(item.facts)]
            for item in ordered
        ],
    ]
    return _sha256_text(_canonical_json(payload))


def derive_file_id(checkout_key: str, revision: str, path: str, digest: str) -> str:
    return _sha256_text(_canonical_json(["lw-index-file-v1", checkout_key, revision, path, digest]))


def derive_symbol_id(file_id: str, local_name: str, kind: SymbolKind | str, occurrence: int) -> str:
    kind_value = kind.value if isinstance(kind, SymbolKind) else kind
    return _sha256_text(_canonical_json(["lw-index-symbol-v1", file_id, local_name, kind_value, occurrence]))


def derive_module_id(file_id: str) -> str:
    return _sha256_text(_canonical_json(["lw-index-module-v1", file_id]))


def current_extractor_key() -> str:
    return extractor_key()


def expected_module_name(path: str) -> str:
    return module_name_for_path(path)
