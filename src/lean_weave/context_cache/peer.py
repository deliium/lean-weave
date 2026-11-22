"""Explicit, trusted Python-facts reuse between two managed worktrees."""

from __future__ import annotations

from dataclasses import dataclass

from lean_weave.context.python_index import PythonFacts
from lean_weave.context.repository import FileSnapshot, SourceScan
from lean_weave.context_cache.facts import FactCache
from lean_weave.context_cache.models import ArtifactKind
from lean_weave.context_cache.service import CachePolicy, SqliteContextCache
from lean_weave.logging_setup import get_logger

log = get_logger("context_cache.peer")


@dataclass
class AuthorizedFactPeer:
    """Short-lived capability; never enumerable and never persisted."""

    local_cache: SqliteContextCache
    peer_cache: SqliteContextCache
    local_scan: SourceScan
    peer_scan: SourceScan
    allowed_paths: frozenset[str]
    policy: CachePolicy
    _closed: bool = False

    def close(self) -> None:
        self._closed = True

    def _snapshot(self, scan: SourceScan, path: str) -> FileSnapshot | None:
        for item in scan.snapshots:
            if item.path == path:
                return item
        return None

    def load(self, snapshot: FileSnapshot) -> PythonFacts | None:
        if self._closed:
            return None
        if snapshot.path not in self.allowed_paths:
            return None
        local = self._snapshot(self.local_scan, snapshot.path)
        peer = self._snapshot(self.peer_scan, snapshot.path)
        if local is None or peer is None:
            return None
        if (
            local.digest != snapshot.digest
            or local.lines != snapshot.lines
            or peer.digest != snapshot.digest
            or peer.lines != snapshot.lines
        ):
            return None
        if not self.local_scan.revalidate() or not self.peer_scan.revalidate():
            return None
        local_facts = FactCache(self.local_cache, policy=self.policy)
        peer_facts = FactCache(self.peer_cache, policy=self.policy)
        outcome = self.peer_cache.lookup(
            peer_facts.key(peer),
            peer_facts.proof(peer),
            codec=peer_facts.codec,
            write_stats=False,
        )
        if not outcome.hit:
            return None
        value = outcome.value
        if not local_facts.codec.validate(value, local_facts.proof(snapshot)):
            return None
        if not self.local_scan.revalidate() or not self.peer_scan.revalidate():
            return None
        log.debug("[ContextCache.peer] outcome=hit kind=%s", ArtifactKind.FACTS.value)
        return value
