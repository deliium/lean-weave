"""Task-aware retrieval candidate and ranking-projection cache."""

from __future__ import annotations

import json
import shutil
from typing import Mapping, Sequence

from lean_weave.config.models import RetrievalWeights
from lean_weave.context.repository import FileSnapshot, SourceScan
from lean_weave.context.retrieval_models import (
    DependencyEvidence,
    RetrievalHit,
    RetrievalRequest,
    RetrievalResult,
)
from lean_weave.context.retrieval_ranking import RankCandidate
from lean_weave.context.search import LiteralLineMatch
from lean_weave.context_cache.models import (
    ArtifactKind,
    CacheBatch,
    CacheCodec,
    CacheKey,
    CacheProof,
    ContextCache,
    _digest,
)
from lean_weave.context_cache.service import CachePolicy
from lean_weave.errors import CodeIndexError
from lean_weave.logging_setup import get_logger

log = get_logger("context_cache.retrieval")

RETRIEVAL_CODEC_VERSION = 1
CANDIDATE_CODEC_VERSION = 1


def lexical_backend() -> str:
    return "rg" if shutil.which("rg") else "python"


def graph_digest(graph) -> str | None:
    if graph is None:
        return None
    payload = [
        graph.revision,
        graph.manifest_sha256,
        [[node.path, node.digest] for node in graph.nodes],
        [[key, list(value)] for key, value in sorted(graph.forward.items())],
        [[key, list(value)] for key, value in sorted(graph.reverse.items())],
    ]
    return _digest("lw-context-graph-v1", payload)


def _candidate_payload(candidate: RankCandidate) -> dict:
    anchor = None
    if candidate._anchor is not None:
        anchor = {
            "path": candidate._anchor.path,
            "line": candidate._anchor.line,
            "primary_mask": candidate._anchor.primary_mask,
            "expanded_mask": candidate._anchor.expanded_mask,
            "phrase": candidate._anchor.phrase,
        }
    return {
        "kind": candidate.kind,
        "path": candidate.path,
        "symbol_id": candidate.symbol_id,
        "qualified_name": candidate.qualified_name,
        "definition_start": candidate.definition_start,
        "definition_end": candidate.definition_end,
        "source_digest": candidate.source_digest,
        "source_kind": candidate.source_kind,
        "signals": dict(candidate.signals),
        "related": [item.model_dump(mode="json") for item in candidate.related],
        "start_line": candidate.start_line,
        "end_line": candidate.end_line,
        "estimated_token_cost": candidate.estimated_token_cost,
        "unrounded_points": candidate._unrounded_points,
        "primary_mask": candidate._primary_mask,
        "expanded_mask": candidate._expanded_mask,
        "phrase": candidate._phrase,
        "anchor": anchor,
    }


def _candidate_from_payload(value: object) -> RankCandidate:
    if not isinstance(value, dict):
        raise ValueError("candidate")
    anchor = value.get("anchor")
    parsed_anchor = None
    if anchor is not None:
        if not isinstance(anchor, dict):
            raise ValueError("anchor")
        parsed_anchor = LiteralLineMatch(
            path=str(anchor["path"]),
            line=int(anchor["line"]),
            primary_mask=int(anchor["primary_mask"]),
            expanded_mask=int(anchor["expanded_mask"]),
            phrase=bool(anchor["phrase"]),
        )
    return RankCandidate(
        kind=str(value["kind"]),
        path=str(value["path"]),
        symbol_id=None if value["symbol_id"] is None else str(value["symbol_id"]),
        qualified_name=(
            None if value["qualified_name"] is None else str(value["qualified_name"])
        ),
        definition_start=value["definition_start"],
        definition_end=value["definition_end"],
        source_digest=str(value["source_digest"]),
        source_kind=str(value["source_kind"]),
        signals={str(k): int(v) for k, v in value["signals"].items()},
        related=[DependencyEvidence.model_validate(item) for item in value["related"]],
        start_line=value["start_line"],
        end_line=value["end_line"],
        estimated_token_cost=int(value["estimated_token_cost"]),
        _unrounded_points=int(value["unrounded_points"]),
        _primary_mask=int(value["primary_mask"]),
        _expanded_mask=int(value["expanded_mask"]),
        _phrase=bool(value["phrase"]),
        _anchor=parsed_anchor,
    )


class RetrievalCodec(CacheCodec[dict]):
    kind = ArtifactKind.RANKING_RESULT

    def __init__(
        self,
        *,
        request: RetrievalRequest,
        top_k: int,
        snapshots: Mapping[str, FileSnapshot],
        max_terms: int,
    ) -> None:
        self.request = request
        self.top_k = top_k
        self.snapshots = snapshots
        self.max_terms = max_terms

    def encode(self, value: dict) -> bytes:
        return json.dumps(
            {
                "result": value["result"].model_dump(mode="json"),
                "selected": [_candidate_payload(item) for item in value["selected"]],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

    def decode(self, payload: bytes) -> dict:
        raw = json.loads(payload.decode("utf-8"))
        if not isinstance(raw, dict) or set(raw) != {"result", "selected"}:
            raise ValueError("retrieval")
        result = RetrievalResult.model_validate(raw["result"])
        selected = tuple(_candidate_from_payload(item) for item in raw["selected"])
        return {"result": result, "selected": selected}

    def validate(self, value: dict, proof: CacheProof) -> bool:
        result = value.get("result")
        selected = value.get("selected")
        if not isinstance(result, RetrievalResult) or not isinstance(selected, tuple):
            return False
        if result.task_kind != self.request.task_kind:
            return False
        if result.kind != self.request.kind:
            return False
        if result.top_k != self.top_k:
            return False
        mask_bound = 1 << max(1, self.max_terms)
        for hit in result.hits:
            snapshot = self.snapshots.get(hit.path)
            if snapshot is None or snapshot.digest != hit.source_digest:
                return False
            if hit.start_line is not None and hit.start_line < 1:
                return False
            if hit.end_line is not None and hit.end_line < (hit.start_line or 1):
                return False
        for candidate in selected:
            snapshot = self.snapshots.get(candidate.path)
            if snapshot is None or snapshot.digest != candidate.source_digest:
                return False
            if candidate.start_line is not None and candidate.start_line < 1:
                return False
            if candidate.end_line is not None and candidate.end_line < (
                candidate.start_line or 1
            ):
                return False
            if candidate._primary_mask < 0 or candidate._primary_mask > mask_bound:
                return False
            if candidate._expanded_mask < 0 or candidate._expanded_mask > mask_bound:
                return False
            if candidate._anchor is not None:
                if candidate._anchor.path != candidate.path:
                    return False
                if not 1 <= candidate._anchor.line <= max(snapshot.lines, 1):
                    return False
                if candidate._anchor.primary_mask > mask_bound:
                    return False
                if candidate._anchor.expanded_mask > mask_bound:
                    return False
        return True


class CandidateCodec(CacheCodec[dict]):
    """Path-local base candidates, never graph-propagated or rendered text."""

    kind = ArtifactKind.RETRIEVAL_CANDIDATES

    def __init__(
        self,
        *,
        path: str,
        snapshot: FileSnapshot,
        definitions: Sequence,
        lexical,
        max_terms: int,
    ) -> None:
        self.path = path
        self.snapshot = snapshot
        self.definitions = {
            item.symbol_id: item for item in definitions
        }
        self.lexical = lexical
        self.max_terms = max_terms

    def encode(self, value: dict) -> bytes:
        return json.dumps(
            {
                "file": _candidate_payload(value["file"]),
                "symbols": [_candidate_payload(item) for item in value["symbols"]],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

    def decode(self, payload: bytes) -> dict:
        raw = json.loads(payload.decode("utf-8"))
        if not isinstance(raw, dict) or set(raw) != {"file", "symbols"}:
            raise ValueError("candidates")
        return {
            "file": _candidate_from_payload(raw["file"]),
            "symbols": tuple(
                _candidate_from_payload(item) for item in raw["symbols"]
            ),
        }

    def validate(self, value: dict, proof: CacheProof) -> bool:
        if set(value) != {"file", "symbols"}:
            return False
        file_candidate = value["file"]
        if file_candidate.kind != "file" or file_candidate.path != self.path:
            return False
        if file_candidate.source_digest != self.snapshot.digest:
            return False
        bound = 1 << max(1, self.max_terms)
        for candidate in (file_candidate, *value["symbols"]):
            if candidate.path != self.path:
                return False
            if candidate.source_digest != self.snapshot.digest:
                return False
            if candidate._primary_mask < 0 or candidate._primary_mask > bound:
                return False
            if candidate._expanded_mask < 0 or candidate._expanded_mask > bound:
                return False
            if candidate._anchor is not None:
                if candidate._anchor.path != self.path:
                    return False
                if not 1 <= candidate._anchor.line <= max(self.snapshot.lines, 1):
                    return False
            if candidate.kind == "symbol":
                definition = self.definitions.get(candidate.symbol_id or "")
                if definition is None:
                    return False
                if candidate.definition_start != definition.start_line:
                    return False
                if candidate.definition_end != definition.end_line:
                    return False
        return True


class RetrievalCandidateCache:
    """Reuse per-file base signals for unchanged admitted paths."""

    def __init__(self, cache: ContextCache, *, policy: CachePolicy) -> None:
        self.cache = cache
        self.policy = policy

    def key(self, path: str, prepared) -> CacheKey:
        return CacheKey.create(
            ArtifactKind.RETRIEVAL_CANDIDATES,
            algorithm={"codec": CANDIDATE_CODEC_VERSION, "base_ranking": 1},
            config={"policy": self.policy.digest(), "lexical": lexical_backend()},
            inputs={
                "path": path,
                "phrase": prepared.phrase,
                "selectors": list(prepared.selectors),
                "primary": list(prepared.primary),
                "expanded": list(prepared.expanded),
                "primary_capped": prepared.primary_capped,
                "expanded_capped": prepared.expanded_capped,
            },
        )

    def proof(self, snapshot: FileSnapshot, definitions, lexical) -> CacheProof:
        return CacheProof.create(
            ArtifactKind.RETRIEVAL_CANDIDATES,
            inputs={
                "digest": snapshot.digest,
                "lines": snapshot.lines,
                "definitions": [
                    [
                        item.symbol_id,
                        item.qualified_name,
                        item.simple_name,
                        item.kind,
                        item.start_line,
                        item.end_line,
                        item.source_kind,
                    ]
                    for item in definitions
                ],
                "lexical": None
                if lexical is None
                else [
                    lexical.primary_mask,
                    lexical.expanded_mask,
                    lexical.phrase,
                    [
                        [
                            anchor.line,
                            anchor.primary_mask,
                            anchor.expanded_mask,
                            anchor.phrase,
                        ]
                        for anchor in lexical.anchors
                    ],
                ],
            },
        )

    def load(
        self,
        *,
        path: str,
        snapshot: FileSnapshot,
        definitions,
        lexical,
        prepared,
        batch: CacheBatch | None,
    ) -> dict | None:
        codec = CandidateCodec(
            path=path,
            snapshot=snapshot,
            definitions=definitions,
            lexical=lexical,
            max_terms=max(2, len(prepared.primary) + len(prepared.expanded) + 4),
        )
        outcome = self.cache.lookup(
            self.key(path, prepared), self.proof(snapshot, definitions, lexical), codec=codec
        )
        return outcome.value if outcome.hit else None

    def publish(
        self,
        *,
        path: str,
        snapshot: FileSnapshot,
        definitions,
        lexical,
        prepared,
        batch: CacheBatch | None,
        value: dict,
    ) -> bool:
        if batch is None:
            return False
        codec = CandidateCodec(
            path=path,
            snapshot=snapshot,
            definitions=definitions,
            lexical=lexical,
            max_terms=max(2, len(prepared.primary) + len(prepared.expanded) + 4),
        )
        return self.cache.publish(
            self.key(path, prepared),
            self.proof(snapshot, definitions, lexical),
            value,
            codec=codec,
            batch=batch,
        )


class RetrievalCache:
    """Whole-result reuse keyed by task inputs and exact live evidence."""

    def __init__(self, cache: ContextCache, *, policy: CachePolicy) -> None:
        self.cache = cache
        self.policy = policy

    def key(
        self,
        request: RetrievalRequest,
        *,
        top_k: int,
        weights: Mapping[str, int],
        allowed_paths: frozenset[str] | None,
    ) -> CacheKey:
        return CacheKey.create(
            ArtifactKind.RANKING_RESULT,
            algorithm={"codec": RETRIEVAL_CODEC_VERSION, "ranking": 1, "cost": 1},
            config={
                "policy": self.policy.digest(),
                "weights": {name: int(value) for name, value in sorted(weights.items())},
                "top_k": top_k,
                "allowed": None if allowed_paths is None else sorted(allowed_paths),
                "lexical": lexical_backend(),
            },
            inputs={
                "query": request.query,
                "task_kind": request.task_kind,
                "kind": request.kind,
                "top_k": request.top_k,
            },
        )

    def proof(
        self,
        scan: SourceScan,
        *,
        graph,
        git_paths: frozenset[str] | None,
        degraded: Sequence[str],
    ) -> CacheProof:
        return CacheProof.create(
            ArtifactKind.RANKING_RESULT,
            inputs={
                "snapshots": sorted(
                    [item.path, item.digest, item.lines] for item in scan.snapshots
                ),
                "graph": graph_digest(graph),
                "git": None if git_paths is None else sorted(git_paths),
                "degraded": sorted(set(degraded)),
            },
        )

    def load(
        self,
        request: RetrievalRequest,
        *,
        top_k: int,
        weights: Mapping[str, int],
        allowed_paths: frozenset[str] | None,
        scan: SourceScan,
        graph,
        git_paths: frozenset[str] | None,
        degraded: Sequence[str],
        batch: CacheBatch | None,
    ) -> dict | None:
        snapshots = {item.path: item for item in scan.snapshots}
        if allowed_paths is not None:
            snapshots = {
                path: item for path, item in snapshots.items() if path in allowed_paths
            }
        codec = RetrievalCodec(
            request=request,
            top_k=top_k,
            snapshots=snapshots,
            max_terms=max(2, len(request.query.split()) + 16),
        )
        key = self.key(
            request, top_k=top_k, weights=weights, allowed_paths=allowed_paths
        )
        proof = self.proof(scan, graph=graph, git_paths=git_paths, degraded=degraded)
        outcome = self.cache.lookup(key, proof, codec=codec)
        if outcome.hit:
            return outcome.value
        return None

    def publish(
        self,
        request: RetrievalRequest,
        *,
        top_k: int,
        weights: Mapping[str, int],
        allowed_paths: frozenset[str] | None,
        scan: SourceScan,
        graph,
        git_paths: frozenset[str] | None,
        degraded: Sequence[str],
        batch: CacheBatch | None,
        result: RetrievalResult,
        selected: Sequence[RankCandidate],
    ) -> bool:
        if batch is None or "lexical_python_fallback" in degraded:
            return False
        snapshots = {item.path: item for item in scan.snapshots}
        if allowed_paths is not None:
            snapshots = {
                path: item for path, item in snapshots.items() if path in allowed_paths
            }
        codec = RetrievalCodec(
            request=request,
            top_k=top_k,
            snapshots=snapshots,
            max_terms=max(2, len(request.query.split()) + 16),
        )
        key = self.key(
            request, top_k=top_k, weights=weights, allowed_paths=allowed_paths
        )
        proof = self.proof(scan, graph=graph, git_paths=git_paths, degraded=degraded)
        return self.cache.publish(
            key,
            proof,
            {"result": result, "selected": tuple(selected)},
            codec=codec,
            batch=batch,
        )
