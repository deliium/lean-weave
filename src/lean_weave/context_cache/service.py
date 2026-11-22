"""Checkout-local context-cache service: leases, epochs, and maintenance."""

from __future__ import annotations

import contextlib
import fcntl
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from lean_weave.code_index.identity import (
    CacheLocation,
    context_cache_location,
    describe_leaf,
    ensure_cache_location,
    open_lock,
    resolve_checkout,
)
from lean_weave.config.models import ContextCacheConfig, ContextConfig
from lean_weave.context_cache.models import (
    PAYLOAD_BYTES_MAX,
    ArtifactKind,
    CacheBatch,
    CacheCodec,
    CacheEvent,
    CacheHealth,
    CacheKey,
    CacheOutcome,
    CacheProof,
    CacheReason,
    CacheResult,
    CacheStats,
    CacheStatus,
    PROOF_DOMAIN,
    _digest,
)
from lean_weave.context_cache.store import (
    SCHEMA_VERSION,
    SqliteCacheStore,
    new_epoch,
    read_epoch,
    row_checksum,
    write_epoch,
    _connect,
)
from lean_weave.errors import CodeIndexError
from lean_weave.logging_setup import get_logger

log = get_logger("context_cache")

POLICY_CONTRACT_VERSION = 1


def _sleep_short() -> None:
    time.sleep(0.01)


@dataclass(frozen=True)
class CachePolicy:
    """Frozen admission/analysis configuration shared by cache adapters."""

    state_dir: str
    protected_paths: tuple[str, ...]
    exclusions: tuple[str, ...]
    candidate_bytes_max: int
    scan_bytes_max: int
    visited_entries_max: int

    @classmethod
    def from_context(
        cls,
        config: ContextConfig,
        *,
        state_dir: str,
        protected_paths: Iterable[str] = (),
    ) -> "CachePolicy":
        return cls(
            state_dir=state_dir,
            protected_paths=tuple(sorted(set(protected_paths))),
            exclusions=tuple(config.exclusions),
            candidate_bytes_max=config.candidate_bytes_max,
            scan_bytes_max=config.scan_bytes_max,
            visited_entries_max=config.visited_entries_max,
        )

    def payload(self) -> list[object]:
        return [
            "lw-context-policy-v1",
            POLICY_CONTRACT_VERSION,
            self.state_dir,
            list(self.protected_paths),
            list(self.exclusions),
            int(self.candidate_bytes_max),
            int(self.scan_bytes_max),
            int(self.visited_entries_max),
        ]

    def digest(self) -> str:
        return _digest(PROOF_DOMAIN, self.payload())


class _PendingWriter:
    """Bounded writer against one private pending database."""

    def __init__(
        self,
        path: Path,
        *,
        checkout_key: str,
        max_bytes: int,
        max_entries: int,
    ) -> None:
        self.path = path
        self.checkout_key = checkout_key
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self._connection = _connect(path, writable=True, max_bytes=max_bytes)
        self._sequence = 0
        self._entries = 0
        self._bytes = 0

    def publish(self, key: CacheKey, proof: CacheProof, value, *, codec: CacheCodec) -> bool:
        try:
            payload = codec.encode(value)
        except Exception:
            return False
        if len(payload) > PAYLOAD_BYTES_MAX:
            return False
        if self._entries >= self.max_entries or self._bytes + len(payload) > self.max_bytes:
            return False
        checksum = row_checksum(
            checkout_key=self.checkout_key,
            kind=key.kind,
            key_digest=key.digest,
            proof_digest=proof.digest,
            source_revision=proof.source_revision,
            payload=payload,
        )
        self._sequence += 1
        try:
            self._connection.execute(
                "INSERT INTO entries(kind, key_digest, proof_digest, source_revision, "
                "payload, envelope_checksum, byte_length, access_sequence) "
                "VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(kind, key_digest, proof_digest) DO UPDATE SET "
                "source_revision=excluded.source_revision, payload=excluded.payload, "
                "envelope_checksum=excluded.envelope_checksum, "
                "byte_length=excluded.byte_length, access_sequence=excluded.access_sequence",
                (
                    key.kind.value,
                    key.digest,
                    proof.digest,
                    proof.source_revision,
                    payload,
                    checksum,
                    len(payload),
                    self._sequence,
                ),
            )
        except Exception:
            return False
        self._entries += 1
        self._bytes += len(payload) + 256
        return True

    def finish(self) -> None:
        self._connection.execute(
            "UPDATE meta SET sequence=? WHERE singleton=1", (self._sequence,)
        )
        self._connection.execute(
            "INSERT INTO counters(kind, event, count) VALUES(?,?,1) "
            "ON CONFLICT(kind, event) DO UPDATE SET count=count+1",
            (ArtifactKind.FACTS.value, CacheEvent.REBUILD.value),
        )
        self._connection.close()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._connection.close()


class SqliteContextCache:
    """One enabled cache namespace bound to a checkout identity."""

    enabled = True

    def __init__(
        self,
        root: Path,
        *,
        config: ContextCacheConfig | None = None,
        context_config: ContextConfig | None = None,
        state_dir: str = ".lean-weave",
        protected_paths: Iterable[str] = (),
        cache_base: Path | None = None,
    ) -> None:
        self.root = Path(root)
        self.config = config if config is not None else ContextCacheConfig()
        self.context_config = context_config if context_config is not None else ContextConfig()
        self.state_dir = state_dir
        self.protected_paths = tuple(sorted(set(protected_paths)))
        self.cache_base = cache_base
        self.policy = CachePolicy.from_context(
            self.context_config,
            state_dir=state_dir,
            protected_paths=self.protected_paths,
        )

    # ------------------------------------------------------------------
    # Location and leases
    # ------------------------------------------------------------------
    def _location(self) -> CacheLocation:
        identity = resolve_checkout(self.root)
        return context_cache_location(
            identity, cache_base=self.cache_base, source_root=self.root
        )

    @contextlib.contextmanager
    def _shared_lease(self) -> Iterator[int | None]:
        location = self._location()
        kind = describe_leaf(location.lock)
        if kind == "unsafe":
            raise CodeIndexError("unsafe_cache")
        if kind == "missing":
            if describe_leaf(location.database) == "present":
                raise CodeIndexError("storage_unavailable")
            yield None
            return
        fd = open_lock(location.lock, create=False)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CodeIndexError("busy") from None
            except OSError:
                raise CodeIndexError("storage_unavailable") from None
            try:
                yield fd
            finally:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def _exclusive_lease(self) -> Iterator[int]:
        location = self._location()
        ensure_cache_location(location)
        fd = open_lock(location.lock, create=True)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CodeIndexError("busy") from None
            except OSError:
                raise CodeIndexError("storage_unavailable") from None
            try:
                yield fd
            finally:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _initialize_epoch(self) -> bytes:
        with self._exclusive_lease() as fd:
            current = read_epoch(fd)
            if current is None:
                current = new_epoch()
                write_epoch(fd, current)
            return current

    def _store(self, location: CacheLocation | None = None) -> SqliteCacheStore:
        return SqliteCacheStore(
            location or self._location(),
            max_bytes=self.config.max_bytes,
            max_entries=self.config.max_entries,
        )

    # ------------------------------------------------------------------
    # Batch
    # ------------------------------------------------------------------
    def begin_batch(self) -> CacheBatch:
        try:
            with self._shared_lease() as fd:
                epoch = None if fd is None else read_epoch(fd)
            if epoch is None:
                epoch = self._initialize_epoch()
            return CacheBatch(epoch)
        except CodeIndexError:
            return CacheBatch(None, usable=False)

    def flush(self, batch: CacheBatch) -> bool:
        if batch is None or batch.closed or not batch.usable:
            return False
        if not batch.rows and not batch.events:
            batch.closed = True
            return True
        location = self._location()
        store = self._store(location)
        try:
            with self._shared_lease() as fd:
                if fd is None:
                    batch.discard()
                    return False
                if read_epoch(fd) != batch.epoch:
                    log.debug("[ContextCache.flush] outcome=discarded reason=epoch")
                    batch.discard()
                    return False
                health, version, _entries, _bytes, _counts, _pending = store.inspect()
                if health is CacheHealth.MISSING:
                    create = True
                elif version == 1:
                    create = False
                elif health is CacheHealth.READY:
                    create = False
                else:
                    batch.discard()
                    return False
            if create:
                self._prepare_store()
            elif version == 1:
                self._migrate()
            with self._shared_lease() as fd:
                if fd is None or read_epoch(fd) != batch.epoch:
                    batch.discard()
                    return False
                rows = [
                    (
                        kind,
                        key.digest,
                        proof.digest,
                        proof.source_revision,
                        payload,
                        row_checksum(
                            checkout_key=location.checkout_key,
                            kind=kind,
                            key_digest=key.digest,
                            proof_digest=proof.digest,
                            source_revision=proof.source_revision,
                            payload=payload,
                        ),
                    )
                    for key, proof, payload, kind in batch.rows
                ]
                ok = self._publish_with_retry(store, rows, batch.events)
            batch.closed = True
            return ok
        except CodeIndexError as exc:
            log.debug("[ContextCache.flush] outcome=bypass reason=%s", exc.code)
            batch.discard()
            return False

    # ------------------------------------------------------------------
    # Lookup / publish
    # ------------------------------------------------------------------
    def lookup(
        self,
        key: CacheKey,
        proof: CacheProof,
        *,
        codec: CacheCodec,
        write_stats: bool = True,
    ) -> CacheOutcome:
        if key.kind is not proof.kind:
            return CacheOutcome(CacheResult.BYPASS, None, CacheReason.INVALID_INPUT)
        try:
            location = self._location()
        except CodeIndexError as exc:
            return CacheOutcome(CacheResult.BYPASS, None, _reason(exc.code))
        store = self._store(location)
        try:
            if store.unsafe():
                return CacheOutcome(CacheResult.BYPASS, None, CacheReason.UNSAFE)
            with self._shared_lease() as fd:
                if fd is None:
                    return CacheOutcome(CacheResult.MISS, None, CacheReason.ABSENT)
                if read_epoch(fd) is None:
                    return CacheOutcome(CacheResult.BYPASS, None, CacheReason.UNAVAILABLE)
                if describe_leaf(location.database) != "present":
                    return CacheOutcome(CacheResult.MISS, None, CacheReason.ABSENT)
                found = store.lookup(key.kind, key.digest, proof.digest)
                if found.row is None:
                    if found.corrupt:
                        if write_stats:
                            store.record_events([(key.kind, CacheEvent.CORRUPT_ENTRY)])
                        return CacheOutcome(
                            CacheResult.INVALIDATED, None, CacheReason.MALFORMED_ROW
                        )
                    if found.other_proof_exists:
                        if write_stats:
                            store.record_events([(key.kind, CacheEvent.INVALIDATION)])
                        return CacheOutcome(
                            CacheResult.INVALIDATED, None, CacheReason.STALE_PROOF
                        )
                    if write_stats:
                        store.record_events([(key.kind, CacheEvent.MISS)])
                    return CacheOutcome(CacheResult.MISS, None, CacheReason.ABSENT)
                row = found.row
                expected = row_checksum(
                    checkout_key=location.checkout_key,
                    kind=key.kind,
                    key_digest=key.digest,
                    proof_digest=proof.digest,
                    source_revision=row.source_revision,
                    payload=row.payload,
                )
                if expected != row.checksum:
                    if write_stats:
                        store.record_events([(key.kind, CacheEvent.CORRUPT_ENTRY)])
                    log.warning("[ContextCache.lookup] outcome=corrupt kind=%s", key.kind.value)
                    return CacheOutcome(
                        CacheResult.INVALIDATED, None, CacheReason.CHECKSUM_MISMATCH
                    )
                try:
                    value = codec.decode(row.payload)
                    valid = codec.validate(value, proof)
                except Exception:
                    if write_stats:
                        store.record_events([(key.kind, CacheEvent.CORRUPT_ENTRY)])
                    return CacheOutcome(
                        CacheResult.INVALIDATED, None, CacheReason.MALFORMED_ROW
                    )
                if not valid:
                    if write_stats:
                        store.record_events([(key.kind, CacheEvent.INVALIDATION)])
                    return CacheOutcome(
                        CacheResult.INVALIDATED, None, CacheReason.STALE_PROOF
                    )
                if write_stats:
                    store.touch([(key.kind, key.digest, proof.digest)])
                    store.record_events([(key.kind, CacheEvent.HIT)])
                return CacheOutcome(CacheResult.HIT, value, None)
        except CodeIndexError as exc:
            return CacheOutcome(CacheResult.BYPASS, None, _reason(exc.code))
        except Exception:
            return CacheOutcome(CacheResult.BYPASS, None, CacheReason.UNAVAILABLE)

    def publish(self, key: CacheKey, proof: CacheProof, value, *, codec: CacheCodec, batch=None) -> bool:
        if key.kind is not proof.kind:
            return False
        try:
            payload = codec.encode(value)
        except Exception:
            return False
        if len(payload) > PAYLOAD_BYTES_MAX:
            return False
        if batch is not None:
            return batch.stage(key, proof, payload, kind=key.kind)
        location = self._location()
        checksum = row_checksum(
            checkout_key=location.checkout_key,
            kind=key.kind,
            key_digest=key.digest,
            proof_digest=proof.digest,
            source_revision=proof.source_revision,
            payload=payload,
        )
        return self._write_rows(
            location,
            [(key.kind, key.digest, proof.digest, proof.source_revision, payload, checksum)],
            [(key.kind, CacheEvent.MISS)],
        )

    def _write_rows(self, location, rows, events) -> bool:
        for attempt in range(50):
            try:
                return self._write_rows_once(location, rows, events)
            except CodeIndexError as exc:
                if exc.code in {"busy", "storage_unavailable"} and attempt < 49:
                    _sleep_short()
                    continue
                log.debug(
                    "[ContextCache.publish] outcome=bypass reason=%s", exc.code
                )
                return False
        return False

    def _write_rows_once(self, location, rows, events) -> bool:
        store = self._store(location)
        prepare = False
        migrate = False
        with self._shared_lease() as fd:
            if fd is None:
                prepare = True
            else:
                health, version, *_rest = store.inspect()
                if health is CacheHealth.MISSING:
                    prepare = True
                elif version == 1:
                    migrate = True
                elif health is not CacheHealth.READY:
                    return False
        if prepare:
            self._prepare_store()
        elif migrate:
            self._migrate()
        with self._shared_lease() as fd:
            if fd is None or read_epoch(fd) is None:
                return False
            return self._publish_with_retry(store, rows, events)

    @staticmethod
    def _publish_with_retry(store, rows, events) -> bool:
        for attempt in range(50):
            try:
                return store.publish(rows, events)
            except CodeIndexError as exc:
                if (
                    exc.code not in {"busy", "storage_unavailable"}
                    or attempt >= 49
                ):
                    raise
                _sleep_short()
        return False

    def _prepare_store(self) -> None:
        location = self._location()
        for attempt in range(50):
            try:
                with self._exclusive_lease() as fd:
                    current = read_epoch(fd)
                    if current is None:
                        write_epoch(fd, new_epoch())
                    store = self._store(location)
                    health, version, _entries, _bytes, _counts, _pending = store.inspect()
                    if health is CacheHealth.MISSING:
                        store.create(checkout_key=location.checkout_key)
                    elif version == 1:
                        store.migrate_v1()
                    return
            except CodeIndexError as exc:
                if exc.code != "busy" or attempt >= 49:
                    raise
                _sleep_short()

    def _migrate(self) -> None:
        location = self._location()
        for attempt in range(50):
            try:
                with self._exclusive_lease() as fd:
                    if read_epoch(fd) is None:
                        write_epoch(fd, new_epoch())
                    self._store(location).migrate_v1()
                    log.info(
                        "[ContextCache.migration] outcome=applied schema=%s",
                        SCHEMA_VERSION,
                    )
                    return
            except CodeIndexError as exc:
                if exc.code != "busy" or attempt >= 49:
                    raise
                _sleep_short()

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------
    def status(self) -> CacheStatus:
        try:
            location = self._location()
        except CodeIndexError as exc:
            return CacheStatus(
                True,
                _health(exc.code),
                max_entries=self.config.max_entries,
                max_bytes=self.config.max_bytes,
            )
        store = self._store(location)
        try:
            with self._shared_lease() as fd:
                if fd is None:
                    return CacheStatus(
                        True,
                        CacheHealth.MISSING,
                        max_entries=self.config.max_entries,
                        max_bytes=self.config.max_bytes,
                    )
                health, version, entries, db_bytes, counts, migration = store.inspect(deep=True)
                if health is CacheHealth.UNSAFE:
                    return CacheStatus(
                        True, health, version, entries, db_bytes,
                        self.config.max_entries, self.config.max_bytes, migration, counts,
                    )
                return CacheStatus(
                    True,
                    health,
                    version,
                    entries,
                    db_bytes,
                    self.config.max_entries,
                    self.config.max_bytes,
                    migration,
                    counts,
                )
        except CodeIndexError as exc:
            return CacheStatus(
                True,
                _health(exc.code),
                max_entries=self.config.max_entries,
                max_bytes=self.config.max_bytes,
            )

    def stats(self) -> CacheStats:
        try:
            location = self._location()
        except CodeIndexError as exc:
            return CacheStats(True, _health(exc.code))
        store = self._store(location)
        try:
            with self._shared_lease() as fd:
                if fd is None:
                    return CacheStats(True, CacheHealth.MISSING)
                if read_epoch(fd) is None:
                    return CacheStats(True, CacheHealth.UNAVAILABLE)
                rows = store.stats()
                events: dict[str, int] = {}
                per_kind: dict[str, dict[str, int]] = {}
                for label, count in rows:
                    kind, _, event = label.partition(":")
                    events[event] = events.get(event, 0) + count
                    per_kind.setdefault(kind, {})[event] = count
                return CacheStats(
                    True,
                    CacheHealth.READY,
                    tuple(sorted(events.items())),
                    tuple(
                        (kind, tuple(sorted(values.items())))
                        for kind, values in sorted(per_kind.items())
                    ),
                )
        except CodeIndexError as exc:
            return CacheStats(True, _health(exc.code))

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------
    def clear(self) -> bool:
        location = self._location()
        if any(describe_leaf(path) == "unsafe" for path in location.leaves.values()):
            raise CodeIndexError("unsafe_cache")
        with self._exclusive_lease() as fd:
            write_epoch(fd, new_epoch())
            removed = False
            for path in (
                location.database,
                location.database_journal,
                location.pending,
                location.pending_journal,
            ):
                mode = describe_leaf(path)
                if mode == "unsafe":
                    raise CodeIndexError("unsafe_cache")
                if mode == "present":
                    try:
                        os.unlink(path)
                        removed = True
                    except OSError:
                        raise CodeIndexError("clear_incomplete") from None
            try:
                dir_fd = os.open(
                    location.checkout_dir,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
                )
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                raise CodeIndexError("clear_incomplete") from None
            log.info(
                "[ContextCache.clear] outcome=%s",
                "cleared" if removed else "missing",
            )
            return removed

    def rebuild(self, seed, *, revalidate) -> CacheStatus:
        location = self._location()
        with self._exclusive_lease() as fd:
            store = self._store(location)
            if any(describe_leaf(path) == "unsafe" for path in location.leaves.values()):
                raise CodeIndexError("unsafe_cache")
            if describe_leaf(location.database_journal) == "present":
                raise CodeIndexError("recovery_required")
            health, version, _entries, _bytes, _counts, _pending = store.inspect()
            if health in {CacheHealth.UNSAFE, CacheHealth.UNAVAILABLE}:
                raise CodeIndexError("storage_unavailable")
            if health is CacheHealth.INCOMPATIBLE:
                raise CodeIndexError("incompatible")
            # A new epoch fences every writer that observed the old generation.
            write_epoch(fd, new_epoch())
            for path in (location.pending, location.pending_journal):
                mode = describe_leaf(path)
                if mode == "unsafe":
                    raise CodeIndexError("unsafe_cache")
                if mode == "present":
                    try:
                        os.unlink(path)
                    except OSError:
                        raise CodeIndexError("clear_incomplete") from None
            try:
                store.create_pending(
                    location.pending, checkout_key=location.checkout_key
                )
                writer = _PendingWriter(
                    location.pending,
                    checkout_key=location.checkout_key,
                    max_bytes=self.config.max_bytes,
                    max_entries=self.config.max_entries,
                )
                try:
                    seed(writer)
                    writer.finish()
                except BaseException:
                    writer.close()
                    raise
                if not store.verify_path(
                    location.pending, checkout_key=location.checkout_key
                ):
                    raise CodeIndexError("publication_uncertain")
                if not revalidate():
                    raise CodeIndexError("incomplete")
                try:
                    pending_fd = os.open(
                        location.pending, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
                    )
                    try:
                        os.fsync(pending_fd)
                    finally:
                        os.close(pending_fd)
                    os.replace(location.pending, location.database)
                    dir_fd = os.open(
                        location.checkout_dir,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
                    )
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except OSError:
                    raise CodeIndexError("publication_uncertain") from None
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(location.pending)
                with contextlib.suppress(OSError):
                    os.unlink(location.pending_journal)
                raise
        log.info("[ContextCache.rebuild] outcome=published")
        return self.status()


def _reason(code: str) -> CacheReason:
    return {
        "unsafe_cache": CacheReason.UNSAFE,
        "busy": CacheReason.BUSY,
        "corrupt": CacheReason.CORRUPT,
        "incompatible": CacheReason.INCOMPATIBLE,
        "capacity_exceeded": CacheReason.CAPACITY,
        "missing": CacheReason.ABSENT,
    }.get(code, CacheReason.UNAVAILABLE)


def _health(code: str) -> CacheHealth:
    return {
        "unsafe_cache": CacheHealth.UNSAFE,
        "busy": CacheHealth.BUSY,
        "corrupt": CacheHealth.CORRUPT,
        "incompatible": CacheHealth.INCOMPATIBLE,
        "capacity_exceeded": CacheHealth.CAPACITY,
        "missing": CacheHealth.MISSING,
    }.get(code, CacheHealth.UNAVAILABLE)
