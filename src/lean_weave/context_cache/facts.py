"""Path-local Python-facts codec and cache adapter."""

from __future__ import annotations

import json
from typing import Callable, Iterable

from lean_weave.context.models import SymbolKind
from lean_weave.context.policy import contains_secret
from lean_weave.context.python_index import (
    DefinitionFact,
    ImportFact,
    PythonFacts,
    extract_python_facts,
    extractor_key,
)
from lean_weave.context.repository import FileSnapshot, Repository
from lean_weave.context_cache.models import (
    ArtifactKind,
    CacheBatch,
    CacheCodec,
    CacheKey,
    CacheProof,
)
from lean_weave.context_cache.models import ContextCache
from lean_weave.context_cache.service import CachePolicy
from lean_weave.errors import CodeIndexError
from lean_weave.logging_setup import get_logger

log = get_logger("context_cache.facts")

FACTS_CODEC_VERSION = 1
_KINDS = frozenset(item.value for item in SymbolKind)


def _valid_name(value: str, *, allow_empty: bool = False) -> bool:
    if not isinstance(value, str):
        return False
    if not value and not allow_empty:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    return not contains_secret(value)


def _validate_facts(facts: PythonFacts, *, path: str) -> bool:
    if not isinstance(facts, PythonFacts):
        return False
    if facts.status not in {"ok", "syntax", "parse_limit"}:
        return False
    if facts.status == "parse_limit":
        return False
    if facts.line_count < 0:
        return False
    for item in facts.definitions:
        if not isinstance(item, DefinitionFact):
            return False
        if item.kind.value not in _KINDS:
            return False
        if not _valid_name(item.local_name) or not _valid_name(item.simple_name):
            return False
        if item.parent is not None and not _valid_name(item.parent):
            return False
        if not _valid_name(item.signature, allow_empty=True):
            return False
        if any(not _valid_name(decorator, allow_empty=True) for decorator in item.decorators):
            return False
        if facts.line_count == 0:
            return False
        if (
            item.start_line < 1
            or item.end_line < item.start_line
            or item.end_line > facts.line_count
        ):
            return False
        if item.traversal_order < 0 or item.occurrence < 0:
            return False
    for item in facts.imports:
        if not isinstance(item, ImportFact):
            return False
        if item.kind not in {"import", "from"}:
            return False
        if item.module is not None and not _valid_name(item.module):
            return False
        if item.level < 0:
            return False
        for name, alias in item.names:
            if not _valid_name(name):
                return False
            if alias is not None and not _valid_name(alias):
                return False
        if item.start_line < 1 or item.end_line < item.start_line:
            return False
    return True


def _facts_payload(value: PythonFacts) -> dict:
    return {
        "status": value.status,
        "line_count": value.line_count,
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
            for item in value.definitions
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
            for item in value.imports
        ],
    }


def _facts_from_payload(payload: object) -> PythonFacts:
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
        definitions.append(
            DefinitionFact(
                local_name=str(item["local_name"]),
                simple_name=str(item["simple_name"]),
                kind=SymbolKind(str(item["kind"])),
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
        names = tuple(
            (str(name), None if alias is None else str(alias))
            for name, alias in item["names"]
        )
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


class FactsCodec(CacheCodec[PythonFacts]):
    kind = ArtifactKind.FACTS

    def encode(self, value: PythonFacts) -> bytes:
        if not _validate_facts(value, path=""):
            raise ValueError("facts")
        return json.dumps(
            _facts_payload(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

    def decode(self, payload: bytes) -> PythonFacts:
        value = _facts_from_payload(json.loads(payload.decode("utf-8")))
        if not _validate_facts(value, path=""):
            raise ValueError("facts")
        return value

    def validate(self, value: PythonFacts, proof: CacheProof) -> bool:
        return _validate_facts(value, path="")


def facts_source_revision(policy: CachePolicy, snapshot: FileSnapshot) -> str:
    proof = CacheProof.create(
        ArtifactKind.FACTS,
        inputs={
            "version": FACTS_CODEC_VERSION,
            "policy": policy.digest(),
            "path": snapshot.path,
            "digest": snapshot.digest,
            "lines": snapshot.lines,
        },
    )
    return proof.digest


def facts_key(policy: CachePolicy, snapshot: FileSnapshot) -> CacheKey:
    return CacheKey.create(
        ArtifactKind.FACTS,
        algorithm={
            "codec": FACTS_CODEC_VERSION,
            "extractor": extractor_key(),
        },
        config={"policy": policy.digest()},
        inputs={"path": snapshot.path},
    )


def facts_proof(policy: CachePolicy, snapshot: FileSnapshot) -> CacheProof:
    return CacheProof.create(
        ArtifactKind.FACTS,
        source_revision=facts_source_revision(policy, snapshot),
        inputs={"digest": snapshot.digest, "lines": snapshot.lines},
    )


class FactCache:
    """Bounded facts adapter over the shared context cache."""

    def __init__(self, cache: ContextCache, *, policy: CachePolicy) -> None:
        self.cache = cache
        self.policy = policy
        self.codec = FactsCodec()

    def key(self, snapshot: FileSnapshot) -> CacheKey:
        return facts_key(self.policy, snapshot)

    def proof(self, snapshot: FileSnapshot) -> CacheProof:
        return facts_proof(self.policy, snapshot)

    def lookup(self, snapshot: FileSnapshot):
        return self.cache.lookup(
            self.key(snapshot), self.proof(snapshot), codec=self.codec
        )

    def publish(self, snapshot: FileSnapshot, facts: PythonFacts, *, batch: CacheBatch) -> bool:
        if facts.status == "parse_limit":
            return False
        return self.cache.publish(
            self.key(snapshot),
            self.proof(snapshot),
            facts,
            codec=self.codec,
            batch=batch,
        )

    def load(
        self,
        snapshot: FileSnapshot,
        *,
        batch: CacheBatch,
        load_index: Callable[[FileSnapshot], PythonFacts] | None = None,
        peer=None,
    ) -> PythonFacts:
        if load_index is not None:
            try:
                return load_index(snapshot)
            except CodeIndexError:
                pass
        outcome = self.lookup(snapshot)
        if outcome.hit:
            return outcome.value  # type: ignore[return-value]
        if peer is not None:
            try:
                peer_value = peer.load(snapshot)
            except Exception:
                peer_value = None
            if peer_value is not None:
                self.publish(snapshot, peer_value, batch=batch)
                return peer_value
        facts = extract_python_facts(snapshot)
        self.publish(snapshot, facts, batch=batch)
        return facts


class CachedFactLoader:
    """One stage-build facts session with an epoch-fenced publication batch."""

    def __init__(
        self,
        cache: ContextCache,
        repository: Repository,
        *,
        policy: CachePolicy,
        index=None,
        peer=None,
    ) -> None:
        self.repository = repository
        self.fact_cache = FactCache(cache, policy=policy)
        self.cache = cache
        self.index = index
        self.peer = peer
        self.batch: CacheBatch | None = None
        self.staged: list[FileSnapshot] = []
        self.memo: dict[tuple[str, str, int], PythonFacts] = {}

    def __enter__(self):
        self.batch = self.cache.begin_batch()
        return self.load

    def load(self, snapshot: FileSnapshot) -> PythonFacts:
        key = (snapshot.path, snapshot.digest, snapshot.lines)
        cached = self.memo.get(key)
        if cached is not None:
            return cached
        if self.index is not None:
            try:
                facts = self.index.scoped_facts(snapshot)
                self.memo[key] = facts
                return facts
            except CodeIndexError:
                pass
        outcome = self.fact_cache.lookup(snapshot)
        if outcome.hit:
            self.memo[key] = outcome.value  # type: ignore[assignment]
            return outcome.value  # type: ignore[return-value]
        peer_value = None
        if self.peer is not None:
            try:
                peer_value = self.peer.load(snapshot)
            except Exception:
                peer_value = None
        if peer_value is not None:
            facts = peer_value
        else:
            facts = extract_python_facts(snapshot)
        if self.batch is not None and facts.status != "parse_limit":
            if self.fact_cache.publish(snapshot, facts, batch=self.batch):
                self.staged.append(snapshot)
        self.memo[key] = facts
        return facts

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.batch is None:
            return False
        if exc_type is not None:
            self.batch.discard()
            return False
        if not self.batch.usable or not self.batch.rows:
            self.batch.discard()
            return False
        if not verify_snapshots(self.repository, self.staged):
            self.batch.discard()
            log.debug("[ContextCache.facts] outcome=discarded reason=witness")
            return False
        flush = getattr(self.cache, "flush", None)
        if flush is not None:
            flush(self.batch)
        return False


def source_revision_for(policy: CachePolicy, snapshots: Iterable[FileSnapshot]) -> str:
    pairs = sorted((item.path, item.digest) for item in snapshots)
    return CacheProof.create(
        ArtifactKind.FACTS,
        inputs={"version": FACTS_CODEC_VERSION, "policy": policy.digest(), "paths": pairs},
    ).digest


def verify_snapshot(repository: Repository, snapshot: FileSnapshot) -> bool:
    return verify_snapshots(repository, [snapshot])


def verify_snapshots(
    repository: Repository, snapshots: Iterable[FileSnapshot]
) -> bool:
    """Re-admit selected paths through one live descriptor-relative view."""

    selected = list(snapshots)
    if not selected:
        return True
    fresh = Repository(
        repository.root,
        repository.config,
        state_dir=repository.state_dir,
        strict_source=repository.strict_source,
        protected_paths=repository.protected_paths,
    )
    try:
        for snapshot in selected:
            current = fresh.try_read(snapshot.path)
            if current is None:
                return False
            if current.digest != snapshot.digest or current.lines != snapshot.lines:
                return False
        return fresh.inventory().complete
    except Exception:
        return False
