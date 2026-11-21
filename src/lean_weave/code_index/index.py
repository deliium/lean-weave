"""Incremental reconciliation, checked lookup, and clear for the source index."""

from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path
from typing import Callable, Iterable, Iterator

from lean_weave.code_index.identity import (
    IndexLocation,
    describe_leaf,
    ensure_location,
    index_location,
    open_lock,
    resolve_checkout,
)
from lean_weave.code_index.models import (
    INDEX_SCHEMA_VERSION,
    MODULE_KIND,
    FileRecord,
    Freshness,
    IndexStatus,
    IndexUpdate,
    IndexedSymbol,
    StoreHealth,
    SymbolLookup,
    derive_file_id,
    derive_module_id,
    derive_symbol_id,
    policy_key,
    source_revision,
)
from lean_weave.code_index.sqlite_store import SqliteIndexStore
from lean_weave.config.models import ContextConfig
from lean_weave.context.policy import contains_secret
from lean_weave.context.python_index import (
    PythonFacts,
    extract_python_facts,
    extractor_key,
    module_name_for_path,
)
from lean_weave.context.repository import (
    FileSnapshot,
    Repository,
    validate_index_source_path,
)
from lean_weave.errors import CodeIndexError, ForbiddenContext, InvalidContextRequest
from lean_weave.logging_setup import get_logger

log = get_logger("code_index")

_MAX_NAME_BYTES = 4096


@contextlib.contextmanager
def _read_lease(location: IndexLocation) -> Iterator[None]:
    """Acquire a shared lease or report the missing/unsafe layout."""

    lock_kind = describe_leaf(location.lock)
    if lock_kind == "unsafe":
        raise CodeIndexError("unsafe_cache")
    if lock_kind == "missing":
        db_kind = describe_leaf(location.database)
        if db_kind == "present":
            raise CodeIndexError("storage_unavailable")
        raise CodeIndexError("missing")
    fd = open_lock(location.lock, create=False)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CodeIndexError("busy") from None
        except OSError:
            raise CodeIndexError("storage_unavailable") from None
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def _write_lease(location: IndexLocation) -> Iterator[None]:
    ensure_location(location)
    fd = open_lock(location.lock, create=True)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CodeIndexError("busy") from None
        except OSError:
            raise CodeIndexError("storage_unavailable") from None
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise InvalidContextRequest("Index symbol selector must be non-empty")
    try:
        encoded = name.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise InvalidContextRequest("Index symbol selector is not valid UTF-8") from None
    if len(encoded) > _MAX_NAME_BYTES:
        raise InvalidContextRequest("Index symbol selector is too long")
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise InvalidContextRequest("Index symbol selector contains control characters")
    if contains_secret(name):
        raise ForbiddenContext("Index symbol selector was rejected by the credential filter")
    return name


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class CodeIndex:
    """Persistent, disposable source index for one canonical checkout."""

    def __init__(
        self,
        root: Path,
        *,
        context_config: ContextConfig | None = None,
        state_dir: str = ".lean-weave",
        protected_paths: Iterable[str] = (),
        cache_base: Path | None = None,
    ) -> None:
        self.root = Path(root)
        self.context_config = context_config if context_config is not None else ContextConfig()
        self.state_dir = state_dir
        self.protected_paths = tuple(sorted(set(protected_paths)))
        self.cache_base = cache_base

    # ------------------------------------------------------------------
    # Lazy wiring
    # ------------------------------------------------------------------
    def _repository(self) -> Repository:
        return Repository(
            self.root,
            self.context_config,
            state_dir=self.state_dir,
            strict_source=True,
            protected_paths=self.protected_paths,
        )

    def _location(self) -> IndexLocation:
        identity = resolve_checkout(self.root)
        return index_location(identity, cache_base=self.cache_base, source_root=self.root)

    def _policy_key(self) -> str:
        return policy_key(
            state_path=self.state_dir,
            protected_paths=self.protected_paths,
            exclusions=self.context_config.exclusions,
            candidate_bytes_max=self.context_config.candidate_bytes_max,
        )

    # ------------------------------------------------------------------
    # Build / update
    # ------------------------------------------------------------------
    def build(self) -> IndexUpdate:
        return self._reconcile(require_existing=False)

    def update(self) -> IndexUpdate:
        return self._reconcile(require_existing=True)

    def _reconcile(self, *, require_existing: bool) -> IndexUpdate:
        location = self._location()
        with _write_lease(location):
            store = SqliteIndexStore(location)
            health, header, old_records = store.inspect()
            if health is StoreHealth.UNAVAILABLE:
                if store.requires_recovery():
                    raise CodeIndexError("recovery_required")
                raise CodeIndexError("storage_unavailable")
            if health in {StoreHealth.INCOMPATIBLE, StoreHealth.CORRUPT}:
                raise CodeIndexError(health.value)
            if require_existing and health is StoreHealth.MISSING:
                raise CodeIndexError("missing")
            existing = {item.path: item for item in old_records} if health is StoreHealth.HEALTHY else {}
            extractor = extractor_key()
            policy = self._policy_key()
            with self._repository().scan_python_sources() as scan:
                if not scan.complete:
                    log.warning("[CodeIndex.reconcile] outcome=incomplete")
                    raise CodeIndexError("incomplete")
                snapshots = {item.path: item for item in scan.snapshots}
                revision = source_revision(policy, {item.path: item.digest for item in scan.snapshots})
                if (
                    health is StoreHealth.HEALTHY
                    and header is not None
                    and header.revision == revision
                    and header.policy_key == policy
                    and header.extractor_key == extractor
                    and all(
                        item.path in existing
                        and existing[item.path].digest == item.digest
                        and existing[item.path].size_bytes == item.size_bytes
                        for item in scan.snapshots
                    )
                ):
                    counts = {"unchanged": len(snapshots)}
                    counts.update(scan.omission_counts)
                    log.info(
                        "[CodeIndex.reconcile] outcome=unchanged files=%s revision=%s",
                        len(snapshots),
                        revision,
                    )
                    return IndexUpdate(revision, INDEX_SCHEMA_VERSION, counts)
                records, counts = self._plan_generation(
                    snapshots=snapshots,
                    existing=existing,
                    extractor=extractor,
                    header_extractor=None if header is None else header.extractor_key,
                    revision=revision,
                    checkout_key=location.checkout_key,
                )
                counts.update(scan.omission_counts)
                if not scan.revalidate():
                    log.warning("[CodeIndex.reconcile] outcome=incomplete reason=witness")
                    raise CodeIndexError("incomplete")
                if health is StoreHealth.MISSING:
                    self._publish_first_build(
                        store, location, records, revision, policy, extractor, scan
                    )
                else:
                    try:
                        store.write_generation(
                            revision=revision,
                            policy_key_value=policy,
                            extractor_key_value=extractor,
                            files=records,
                        )
                    except CodeIndexError as exc:
                        if exc.code == "unsafe_facts":
                            raise
                        raise
                    except ValueError:
                        raise CodeIndexError("unsafe_facts") from None
                log.info(
                    "[CodeIndex.reconcile] outcome=committed files=%s parsed=%s revision=%s",
                    len(records),
                    counts.get("parsed", 0),
                    revision,
                )
                return IndexUpdate(revision, INDEX_SCHEMA_VERSION, counts)

    def _plan_generation(
        self,
        *,
        snapshots: dict[str, FileSnapshot],
        existing: dict[str, FileRecord],
        extractor: str,
        header_extractor: str | None,
        revision: str,
        checkout_key: str,
    ) -> tuple[tuple[FileRecord, ...], dict[str, int]]:
        old_paths = set(existing)
        new_paths = set(snapshots)
        departed = sorted(old_paths - new_paths)
        appeared = sorted(new_paths - old_paths)
        old_digest_counts: dict[str, int] = {}
        for item in existing.values():
            old_digest_counts[item.digest] = old_digest_counts.get(item.digest, 0) + 1
        new_digest_counts: dict[str, int] = {}
        for item in snapshots.values():
            new_digest_counts[item.digest] = new_digest_counts.get(item.digest, 0) + 1

        # Pair only digests that occur exactly once in both whole manifests.
        movable: dict[str, tuple[str, str]] = {}
        for path in departed:
            digest = existing[path].digest
            if old_digest_counts.get(digest) != 1 or new_digest_counts.get(digest) != 1:
                continue
            candidate = next((item for item in appeared if snapshots[item].digest == digest), None)
            if candidate is not None:
                movable[digest] = (path, candidate)

        mover_by_old = {old: new for old, new in movable.values()}
        mover_by_new = {new: old for old, new in movable.values()}
        counts: dict[str, int] = {
            "added": 0,
            "modified": 0,
            "moved": 0,
            "removed": 0,
            "unchanged": 0,
            "invalidated": 0,
            "parsed": 0,
            "syntax_failed": 0,
        }
        records: list[FileRecord] = []
        used_ids: set[str] = set()
        compatibility = header_extractor is None or header_extractor == extractor

        def parse(snapshot: FileSnapshot) -> PythonFacts:
            counts["parsed"] += 1
            facts = extract_python_facts(snapshot)
            if facts.status != "ok":
                counts["syntax_failed"] += 1
            if not compatibility:
                counts["invalidated"] += 1
            return facts

        for path in sorted(new_paths):
            snapshot = snapshots[path]
            old = existing.get(path)
            if old is not None:
                if old.digest == snapshot.digest:
                    counts["unchanged"] += 1
                    facts = old.facts if compatibility else parse(snapshot)
                    file_id = old.file_id
                else:
                    counts["modified"] += 1
                    facts = parse(snapshot)
                    file_id = old.file_id
            else:
                old_path = mover_by_new.get(path)
                if old_path is not None and old_path in existing:
                    counts["moved"] += 1
                    source = existing[old_path]
                    facts = source.facts if compatibility else parse(snapshot)
                    file_id = source.file_id
                else:
                    counts["added"] += 1
                    facts = parse(snapshot)
                    file_id = derive_file_id(
                        checkout_key, revision, path, snapshot.digest
                    )
            if file_id in used_ids:
                raise CodeIndexError("storage_unavailable")
            used_ids.add(file_id)
            record = FileRecord(
                path=path,
                file_id=file_id,
                module_name=module_name_for_path(path),
                digest=snapshot.digest,
                size_bytes=snapshot.size_bytes,
                line_count=snapshot.lines,
                parse_status=facts.status,
                facts=facts,
            )
            records.append(record)
        counts["removed"] = len(departed) - len(movable)
        return tuple(records), counts

    def _publish_first_build(
        self,
        store: SqliteIndexStore,
        location: IndexLocation,
        records: tuple[FileRecord, ...],
        revision: str,
        policy: str,
        extractor: str,
        scan,
    ) -> None:
        pending = location.pending
        pending_journal = location.pending_journal
        for path in (pending, pending_journal):
            kind = describe_leaf(path)
            if kind == "unsafe":
                raise CodeIndexError("unsafe_cache")
            if kind == "present":
                try:
                    os.unlink(path)
                except OSError:
                    raise CodeIndexError("clear_incomplete") from None
        try:
            store.create_pending(
                pending,
                revision=revision,
                policy_key_value=policy,
                extractor_key_value=extractor,
                files=records,
            )
        except ValueError:
            raise CodeIndexError("unsafe_facts") from None
        try:
            if describe_leaf(pending_journal) == "present":
                raise CodeIndexError("storage_unavailable")
            _fsync_file(pending)
            if not scan.revalidate():
                raise CodeIndexError("incomplete")
            os.replace(pending, location.database)
        except CodeIndexError:
            with contextlib.suppress(OSError):
                os.unlink(pending)
            raise
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(pending)
            raise CodeIndexError("storage_unavailable") from None
        try:
            _fsync_dir(location.checkout_dir)
        except OSError:
            raise CodeIndexError("publication_uncertain") from None

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def status(self) -> IndexStatus:
        location = self._location()
        health = StoreHealth.MISSING
        header = None
        try:
            with _read_lease(location):
                store = SqliteIndexStore(location)
                health, header, _records = store.inspect()
        except CodeIndexError as exc:
            if exc.code == "missing":
                return IndexStatus(StoreHealth.MISSING, Freshness.UNKNOWN)
            health = StoreHealth.UNAVAILABLE
        if health is not StoreHealth.HEALTHY or header is None:
            return IndexStatus(health, Freshness.UNKNOWN)
        if header.policy_key != self._policy_key() or header.extractor_key != extractor_key():
            return IndexStatus(
                health,
                Freshness.STALE,
                header.revision,
                header.schema_version,
                header.files,
                header.symbols,
                header.imports,
            )
        freshness = self._scan_freshness(header.revision)
        return IndexStatus(
            health,
            freshness,
            header.revision,
            header.schema_version,
            header.files,
            header.symbols,
            header.imports,
        )

    def _scan_freshness(self, expected: str) -> Freshness:
        try:
            with self._repository().scan_python_sources() as scan:
                if not scan.complete:
                    return Freshness.UNKNOWN
                revision = source_revision(
                    self._policy_key(),
                    {item.path: item.digest for item in scan.snapshots},
                )
                if not scan.revalidate():
                    return Freshness.UNKNOWN
                return Freshness.CURRENT if revision == expected else Freshness.STALE
        except CodeIndexError:
            return Freshness.UNKNOWN

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------
    def lookup(
        self,
        name: str,
        *,
        path: str | None = None,
        limit: int | None = None,
    ) -> SymbolLookup:
        selector = _validate_name(name)
        selected_path = None
        if path is not None:
            selected_path = validate_index_source_path(
                path, protected_paths=self.protected_paths
            )
        maximum = self.context_config.search_hits_max
        if limit is None:
            bound = maximum
        elif isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise InvalidContextRequest("Index lookup limit must be a positive integer")
        else:
            bound = min(limit, maximum)
        location = self._location()
        repository = self._repository()
        with _read_lease(location):
            store = SqliteIndexStore(location)
            health, header, records = store.inspect()
            if health is not StoreHealth.HEALTHY or header is None:
                raise CodeIndexError("missing" if health is StoreHealth.MISSING else health.value)
            with repository.scan_python_sources() as scan:
                if not scan.complete:
                    raise CodeIndexError("incomplete")
                policy = self._policy_key()
                revision = source_revision(
                    policy, {item.path: item.digest for item in scan.snapshots}
                )
                if (
                    header.policy_key != policy
                    or header.extractor_key != extractor_key()
                    or revision != header.revision
                ):
                    raise CodeIndexError("stale")
                hits = self._collect_hits(
                    records,
                    header.revision,
                    selector,
                    selected_path,
                    bound,
                )
                if not scan.revalidate():
                    raise CodeIndexError("storage_unavailable")
        return SymbolLookup(
            revision=header.revision,
            hits=tuple(hits[:bound]),
            truncated=len(hits) > bound,
        )

    def _collect_hits(
        self,
        records: tuple[FileRecord, ...],
        revision: str,
        name: str,
        path: str | None,
        limit: int,
    ) -> list[IndexedSymbol]:
        hits: list[tuple[tuple, IndexedSymbol]] = []
        for record in records:
            if path is not None and record.path != path:
                continue
            if record.module_name == name:
                empty = record.line_count == 0
                hits.append(
                    (
                        (record.path, 0 if empty else 1, 0 if empty else record.line_count,
                         record.module_name, MODULE_KIND, 0),
                        IndexedSymbol(
                            file_id=record.file_id,
                            symbol_id=derive_module_id(record.file_id),
                            path=record.path,
                            digest=record.digest,
                            revision=revision,
                            module=record.module_name,
                            name=record.module_name,
                            simple_name=record.module_name.rpartition(".")[2] or record.module_name,
                            kind=MODULE_KIND,
                            parent=None,
                            signature=None,
                            decorators=(),
                            start_line=None if empty else 1,
                            end_line=None if empty else record.line_count,
                            parse_status=record.parse_status,
                        ),
                    )
                )
            for ordinal, item in enumerate(
                sorted(
                    record.facts.definitions,
                    key=lambda fact: (fact.start_line, fact.end_line, fact.traversal_order),
                )
            ):
                qualified = f"{record.module_name}.{item.local_name}" if record.module_name else item.local_name
                if item.simple_name != name and item.local_name != name and qualified != name:
                    continue
                hits.append(
                    (
                        (record.path, item.start_line, item.end_line, qualified, item.kind.value, ordinal),
                        IndexedSymbol(
                            file_id=record.file_id,
                            symbol_id=derive_symbol_id(
                                record.file_id, item.local_name, item.kind, item.occurrence
                            ),
                            path=record.path,
                            digest=record.digest,
                            revision=revision,
                            module=record.module_name,
                            name=qualified,
                            simple_name=item.simple_name,
                            kind=item.kind.value,
                            parent=item.parent,
                            signature=item.signature,
                            decorators=item.decorators,
                            start_line=item.start_line,
                            end_line=item.end_line,
                            parse_status=record.parse_status,
                        ),
                    )
                )
        hits.sort(key=lambda pair: pair[0])
        return [item for _key, item in hits[: limit + 1]]

    # ------------------------------------------------------------------
    # Scoped facts for the ContextProvider
    # ------------------------------------------------------------------
    def scoped_facts(self, snapshot: FileSnapshot) -> PythonFacts:
        location = self._location()
        with _read_lease(location):
            store = SqliteIndexStore(location)
            record = store.scoped_file(
                path=snapshot.path,
                digest=snapshot.digest,
                line_count=snapshot.lines,
                policy_key_value=self._policy_key(),
                extractor_key_value=extractor_key(),
            )
            if record is None:
                raise CodeIndexError("missing")
            return record.facts

    @contextlib.contextmanager
    def fact_loader(self) -> Iterator[Callable[[FileSnapshot], PythonFacts]]:
        """One read lease and one memo table for a single provider build."""

        memo: dict[tuple[str, str, str], PythonFacts] = {}
        extractor = extractor_key()
        policy = self._policy_key()

        def fallback(_snapshot: FileSnapshot) -> PythonFacts:
            raise CodeIndexError("missing")

        try:
            location = self._location()
            lease = _read_lease(location)
            lease.__enter__()
        except CodeIndexError:
            yield fallback
            return
        session = None
        try:
            store = SqliteIndexStore(location)
            session = store.scoped_session(
                policy_key_value=policy,
                extractor_key_value=extractor,
            )
            scoped_load = session.__enter__()

            def load(snapshot: FileSnapshot) -> PythonFacts:
                key = (snapshot.path, snapshot.digest, extractor)
                cached = memo.get(key)
                if cached is not None:
                    return cached
                record = scoped_load(snapshot.path, snapshot.digest, snapshot.lines)
                if record is None:
                    raise CodeIndexError("missing")
                memo[key] = record.facts
                return record.facts

            yield load
        finally:
            if session is not None:
                session.__exit__(None, None, None)
            lease.__exit__(None, None, None)

    # ------------------------------------------------------------------
    # Clear
    # ------------------------------------------------------------------
    def clear(self) -> bool:
        location = self._location()
        lock_kind = describe_leaf(location.lock)
        if lock_kind == "unsafe":
            raise CodeIndexError("unsafe_cache")
        if lock_kind == "missing":
            database_kind = describe_leaf(location.database)
            journal_kind = describe_leaf(location.database_journal)
            if database_kind == "present" or journal_kind == "present":
                raise CodeIndexError("storage_unavailable")
            present = any(
                describe_leaf(leaf) != "missing" for leaf in location.leaves.values()
            )
            if not present:
                return False
        with _write_lease(location):
            for leaf in location.leaves.values():
                if describe_leaf(leaf) == "unsafe":
                    raise CodeIndexError("unsafe_cache")
            removed = False
            database = location.database
            if describe_leaf(database) == "present":
                try:
                    os.unlink(database)
                    removed = True
                    _fsync_dir(location.checkout_dir)
                except OSError:
                    raise CodeIndexError("clear_incomplete") from None
            try:
                for path in (location.database_journal, location.pending, location.pending_journal):
                    if describe_leaf(path) == "present":
                        os.unlink(path)
                        removed = True
                _fsync_dir(location.checkout_dir)
            except OSError:
                raise CodeIndexError("clear_incomplete") from None
            log.info("[CodeIndex.clear] outcome=%s", "cleared" if removed else "missing")
            return removed


