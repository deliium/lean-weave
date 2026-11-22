"""Bounded transactional SQLite storage for deterministic context metadata."""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from lean_weave.code_index.identity import CacheLocation, describe_leaf
from lean_weave.context_cache.models import (
    PAYLOAD_BYTES_MAX,
    ROW_OVERHEAD_BYTES,
    ArtifactKind,
    CacheEvent,
    CacheHealth,
    envelope_checksum,
)
from lean_weave.errors import CodeIndexError
from lean_weave.logging_setup import get_logger

log = get_logger("context_cache.store")

APPLICATION_ID = 0x4C574358  # "LWCX"
SCHEMA_VERSION = 2
V1_SCHEMA_VERSION = 1
PAGE_SIZE = 4096
EPOCH_MAGIC = b"LWCXEPOCH1"
EPOCH_HEX_SIZE = 64
EPOCH_SIZE = len(EPOCH_MAGIC) + EPOCH_HEX_SIZE + 6

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_KINDS = frozenset(item.value for item in ArtifactKind)
_EVENTS = frozenset(item.value for item in CacheEvent)

_OBJECTS_V2 = frozenset(
    {
        ("table", "meta"),
        ("table", "entries"),
        ("table", "counters"),
        ("index", "idx_entries_lru"),
    }
)
_OBJECTS_V1 = frozenset(
    {
        ("table", "meta"),
        ("table", "entries"),
        ("table", "counters"),
    }
)
_COLUMNS_V2 = {
    "meta": ["singleton", "checkout_key", "sequence"],
    "entries": [
        "kind",
        "key_digest",
        "proof_digest",
        "source_revision",
        "payload",
        "envelope_checksum",
        "byte_length",
        "access_sequence",
    ],
    "counters": ["kind", "event", "count"],
}
_COLUMNS_V1 = {
    "meta": ["singleton", "checkout_key", "sequence"],
    "entries": [
        "kind",
        "key_digest",
        "proof_digest",
        "source_revision",
        "payload",
        "envelope_checksum",
        "byte_length",
    ],
    "counters": ["kind", "event", "count"],
}

_SCHEMA_V2 = """
CREATE TABLE meta(
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    checkout_key TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence >= 0)
) STRICT;
CREATE TABLE entries(
    kind TEXT NOT NULL,
    key_digest TEXT NOT NULL,
    proof_digest TEXT NOT NULL,
    source_revision TEXT,
    payload BLOB NOT NULL,
    envelope_checksum TEXT NOT NULL,
    byte_length INTEGER NOT NULL CHECK(byte_length >= 0),
    access_sequence INTEGER NOT NULL CHECK(access_sequence >= 0),
    PRIMARY KEY(kind, key_digest, proof_digest)
) STRICT;
CREATE TABLE counters(
    kind TEXT NOT NULL,
    event TEXT NOT NULL,
    count INTEGER NOT NULL CHECK(count >= 0),
    PRIMARY KEY(kind, event)
) STRICT;
CREATE INDEX idx_entries_lru ON entries(access_sequence, kind, key_digest, proof_digest);
"""


@dataclass(frozen=True)
class StoredRow:
    payload: bytes
    checksum: str
    byte_length: int
    source_revision: str | None


@dataclass(frozen=True)
class RowLookup:
    row: StoredRow | None
    other_proof_exists: bool
    corrupt: bool = False


def _quote(path: Path) -> str:
    return "file:" + str(path).replace("?", "%3f").replace("#", "%23")


def _connect(path: Path, *, writable: bool, max_bytes: int) -> sqlite3.Connection:
    mode = "rw" if writable else "ro"
    try:
        connection = sqlite3.connect(
            f"{_quote(path)}?mode={mode}",
            uri=True,
            isolation_level=None,
            timeout=0,
        )
    except sqlite3.Error:
        raise CodeIndexError("storage_unavailable") from None
    try:
        connection.enable_load_extension(False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA busy_timeout=%d" % (2000 if writable else 0))
        if writable:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA secure_delete=ON")
            connection.execute(
                "PRAGMA max_page_count=%d"
                % max(1, max_bytes // PAGE_SIZE)
            )
        else:
            connection.execute("PRAGMA query_only=ON")
    except sqlite3.Error:
        connection.close()
        raise CodeIndexError("storage_unavailable") from None
    return connection


def _objects(connection: sqlite3.Connection) -> set[tuple[str, str]]:
    rows = connection.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {(str(row[0]), str(row[1])) for row in rows}


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]


def _check_schema(connection: sqlite3.Connection, *, version: int) -> bool:
    if version == SCHEMA_VERSION:
        if _objects(connection) != _OBJECTS_V2:
            return False
        expected = _COLUMNS_V2
    elif version == V1_SCHEMA_VERSION:
        if _objects(connection) != _OBJECTS_V1:
            return False
        expected = _COLUMNS_V1
    else:
        return False
    for table, columns in expected.items():
        if _columns(connection, table) != columns:
            return False
    return True


# ---------------------------------------------------------------------------
# Epoch record retained in the private lock inode.
# ---------------------------------------------------------------------------


def read_epoch(fd: int) -> bytes | None:
    """Return the current maintenance epoch, or None for virgin/unavailable."""

    try:
        record = os.pread(fd, EPOCH_SIZE, 0)
    except OSError:
        return None
    if len(record) != EPOCH_SIZE:
        return None
    if not record.startswith(EPOCH_MAGIC):
        return None
    body = record[len(EPOCH_MAGIC):]
    epoch = body[:EPOCH_HEX_SIZE]
    padding = body[EPOCH_HEX_SIZE:]
    if len(epoch) != EPOCH_HEX_SIZE:
        return None
    if any(byte not in b"0123456789abcdef" for byte in epoch):
        return None
    if any(padding):
        return None
    return record


def new_epoch() -> bytes:
    body = os.urandom(32).hex().encode("ascii")
    return EPOCH_MAGIC + body + b"\x00" * (EPOCH_SIZE - len(EPOCH_MAGIC) - len(body))


def write_epoch(fd: int, epoch: bytes) -> None:
    if len(epoch) != EPOCH_SIZE or not epoch.startswith(EPOCH_MAGIC):
        raise CodeIndexError("storage_unavailable")
    view = memoryview(epoch)
    offset = 0
    while view:
        written = os.pwrite(fd, view, offset)
        if written <= 0:
            raise CodeIndexError("storage_unavailable")
        view = view[written:]
        offset += written
    os.fsync(fd)


class SqliteCacheStore:
    """One validated cache namespace. Callers own the surrounding file lease."""

    def __init__(
        self,
        location: CacheLocation,
        *,
        max_bytes: int,
        max_entries: int,
    ) -> None:
        self.location = location
        self.max_bytes = int(max_bytes)
        self.max_entries = int(max_entries)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------
    def unsafe(self) -> bool:
        if any(describe_leaf(path) == "unsafe" for path in self.location.leaves.values()):
            return True
        if any(describe_leaf(path) == "present" for path in self.location.unsafe_sidecars):
            return True
        db = describe_leaf(self.location.database)
        lock = describe_leaf(self.location.lock)
        pending = describe_leaf(self.location.pending)
        if db == "present" and lock != "present":
            return True
        if db != "present" and pending == "present" and lock != "present":
            return True
        return False

    def db_bytes(self) -> int:
        try:
            return os.lstat(self.location.database).st_size
        except OSError:
            return 0

    def inspect(
        self,
        *,
        deep: bool = False,
    ) -> tuple[CacheHealth, int | None, int, int, tuple[tuple[str, int], ...], bool]:
        """Return health, schema, entries, db bytes, kind counts, migration flag."""

        if self.unsafe():
            return CacheHealth.UNSAFE, None, 0, self.db_bytes(), (), False
        db = describe_leaf(self.location.database)
        if db != "present":
            if (
                describe_leaf(self.location.database_journal) == "present"
                or describe_leaf(self.location.pending) == "present"
            ):
                return CacheHealth.UNAVAILABLE, None, 0, 0, (), False
            return CacheHealth.MISSING, None, 0, 0, (), False
        if describe_leaf(self.location.database_journal) == "present":
            return CacheHealth.UNAVAILABLE, None, 0, self.db_bytes(), (), False
        try:
            connection = _connect(
                self.location.database, writable=False, max_bytes=self.max_bytes
            )
        except CodeIndexError:
            return CacheHealth.UNAVAILABLE, None, 0, self.db_bytes(), (), False
        try:
            if deep:
                row = connection.execute("PRAGMA quick_check").fetchone()
                if row is None or row[0] != "ok":
                    return CacheHealth.CORRUPT, None, 0, self.db_bytes(), (), False
            application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if application_id != APPLICATION_ID:
                return CacheHealth.INCOMPATIBLE, version, 0, self.db_bytes(), (), False
            if version not in {V1_SCHEMA_VERSION, SCHEMA_VERSION}:
                return CacheHealth.INCOMPATIBLE, version, 0, self.db_bytes(), (), False
            if not _check_schema(connection, version=version):
                return CacheHealth.INCOMPATIBLE, version, 0, self.db_bytes(), (), False
            meta = connection.execute(
                "SELECT checkout_key FROM meta WHERE singleton=1"
            ).fetchone()
            if meta is None or str(meta[0]) != self.location.checkout_key:
                return CacheHealth.INCOMPATIBLE, version, 0, self.db_bytes(), (), False
            entries = int(
                connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            )
            counts = tuple(
                (str(row[0]), int(row[1]))
                for row in connection.execute(
                    "SELECT kind, COUNT(*) FROM entries GROUP BY kind ORDER BY kind"
                )
            )
            health = CacheHealth.READY
            if self.db_bytes() > self.max_bytes:
                health = CacheHealth.CAPACITY
            return (
                health,
                version,
                entries,
                self.db_bytes(),
                counts,
                version == V1_SCHEMA_VERSION,
            )
        except (sqlite3.Error, ValueError, TypeError):
            return CacheHealth.CORRUPT, None, 0, self.db_bytes(), (), False
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def lookup(self, kind: ArtifactKind, key_digest: str, proof_digest: str) -> RowLookup:
        if not _DIGEST.fullmatch(key_digest) or not _DIGEST.fullmatch(proof_digest):
            return RowLookup(None, False)
        try:
            connection = _connect(
                self.location.database, writable=False, max_bytes=self.max_bytes
            )
        except CodeIndexError:
            raise
        try:
            application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if (
                application_id != APPLICATION_ID
                or version != SCHEMA_VERSION
                or not _check_schema(connection, version=SCHEMA_VERSION)
            ):
                raise CodeIndexError("incompatible")
            row = connection.execute(
                "SELECT payload, envelope_checksum, byte_length, source_revision "
                "FROM entries WHERE kind=? AND key_digest=? AND proof_digest=?",
                (kind.value, key_digest, proof_digest),
            ).fetchone()
            other = connection.execute(
                "SELECT 1 FROM entries WHERE kind=? AND key_digest=? AND proof_digest<>? LIMIT 1",
                (kind.value, key_digest, proof_digest),
            ).fetchone()
            if row is None:
                return RowLookup(None, other is not None)
            length = int(row["byte_length"])
            if length < 0 or length > PAYLOAD_BYTES_MAX:
                return RowLookup(None, other is not None, True)
            payload = bytes(row["payload"])
            if len(payload) != length:
                return RowLookup(None, other is not None, True)
            return RowLookup(
                StoredRow(
                    payload=payload,
                    checksum=str(row["envelope_checksum"]),
                    byte_length=length,
                    source_revision=(
                        None
                        if row["source_revision"] is None
                        else str(row["source_revision"])
                    ),
                ),
                other is not None,
            )
        except sqlite3.Error:
            raise CodeIndexError("storage_unavailable") from None
        finally:
            connection.close()

    def stats(self) -> tuple[tuple[str, int], ...]:
        try:
            connection = _connect(
                self.location.database, writable=False, max_bytes=self.max_bytes
            )
        except CodeIndexError:
            raise
        try:
            return tuple(
                (f"{row[0]}:{row[1]}", int(row[2]))
                for row in connection.execute(
                    "SELECT kind, event, count FROM counters ORDER BY kind, event"
                )
            )
        except sqlite3.Error:
            raise CodeIndexError("storage_unavailable") from None
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def publish(
        self,
        rows: Iterable[
            tuple[ArtifactKind, str, str, str | None, bytes, str]
        ],
        events: Iterable[tuple[ArtifactKind, CacheEvent]],
    ) -> bool:
        """Insert staged rows and events in one bounded transaction."""

        prepared = list(rows)
        if not prepared:
            return False
        if self.db_bytes() > self.max_bytes:
            return False
        try:
            connection = _connect(
                self.location.database, writable=True, max_bytes=self.max_bytes
            )
        except CodeIndexError:
            raise
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._require_current(connection)
                self._ensure_capacity(connection, prepared)
                sequence = int(
                    connection.execute(
                        "SELECT sequence FROM meta WHERE singleton=1"
                    ).fetchone()[0]
                )
                for kind, key_digest, proof_digest, revision, payload, checksum in prepared:
                    if not _DIGEST.fullmatch(key_digest) or not _DIGEST.fullmatch(
                        proof_digest
                    ):
                        raise ValueError("digest")
                    if len(payload) > PAYLOAD_BYTES_MAX:
                        raise ValueError("payload")
                    sequence += 1
                    connection.execute(
                        "INSERT INTO entries(kind, key_digest, proof_digest, source_revision, "
                        "payload, envelope_checksum, byte_length, access_sequence) "
                        "VALUES(?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(kind, key_digest, proof_digest) DO UPDATE SET "
                        "source_revision=excluded.source_revision, payload=excluded.payload, "
                        "envelope_checksum=excluded.envelope_checksum, "
                        "byte_length=excluded.byte_length, "
                        "access_sequence=excluded.access_sequence",
                        (
                            kind.value,
                            key_digest,
                            proof_digest,
                            revision,
                            sqlite3.Binary(payload),
                            checksum,
                            len(payload),
                            sequence,
                        ),
                    )
                connection.execute(
                    "UPDATE meta SET sequence=? WHERE singleton=1", (sequence,)
                )
                self._bump_events(connection, list(events))
                connection.execute("COMMIT")
                return True
            except BaseException:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        except sqlite3.IntegrityError as exc:
            if "FULL" in str(exc) or "capacity" in str(exc):
                raise CodeIndexError("capacity_exceeded") from None
            raise CodeIndexError("storage_unavailable") from None
        except sqlite3.OperationalError as exc:
            if "FULL" in str(exc) or "capacity" in str(exc):
                raise CodeIndexError("capacity_exceeded") from None
            raise CodeIndexError("storage_unavailable") from None
        except ValueError:
            raise CodeIndexError("unsafe_facts") from None
        finally:
            connection.close()

    def touch(self, keys: Iterable[tuple[ArtifactKind, str, str]]) -> None:
        prepared = list(keys)
        if not prepared:
            return
        if self.db_bytes() > self.max_bytes:
            return
        try:
            connection = _connect(
                self.location.database, writable=True, max_bytes=self.max_bytes
            )
        except CodeIndexError:
            return
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                sequence = int(
                    connection.execute(
                        "SELECT sequence FROM meta WHERE singleton=1"
                    ).fetchone()[0]
                )
                for kind, key_digest, proof_digest in prepared:
                    sequence += 1
                    connection.execute(
                        "UPDATE entries SET access_sequence=? "
                        "WHERE kind=? AND key_digest=? AND proof_digest=?",
                        (sequence, kind.value, key_digest, proof_digest),
                    )
                connection.execute(
                    "UPDATE meta SET sequence=? WHERE singleton=1", (sequence,)
                )
                connection.execute("COMMIT")
            except BaseException:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                return
        except sqlite3.Error:
            return
        finally:
            connection.close()

    def record_events(self, events: Iterable[tuple[ArtifactKind, CacheEvent]]) -> None:
        prepared = list(events)
        if not prepared:
            return
        try:
            connection = _connect(
                self.location.database, writable=True, max_bytes=self.max_bytes
            )
        except CodeIndexError:
            return
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._bump_events(connection, prepared)
                connection.execute("COMMIT")
            except BaseException:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
        except sqlite3.Error:
            pass
        finally:
            connection.close()

    def _bump_events(
        self,
        connection: sqlite3.Connection,
        events: list[tuple[ArtifactKind, CacheEvent]],
    ) -> None:
        for kind, event in events:
            connection.execute(
                "INSERT INTO counters(kind, event, count) VALUES(?,?,1) "
                "ON CONFLICT(kind, event) DO UPDATE SET count=count+1",
                (kind.value, event.value),
            )

    def _require_current(self, connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION or not _check_schema(
            connection, version=SCHEMA_VERSION
        ):
            raise CodeIndexError("incompatible")

    def _ensure_capacity(
        self,
        connection: sqlite3.Connection,
        incoming: list[tuple[ArtifactKind, str, str, str | None, bytes, str]],
    ) -> None:
        def logical_bytes() -> int:
            row = connection.execute(
                "SELECT COALESCE(SUM(byte_length), 0) FROM entries"
            ).fetchone()
            count = int(connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
            return int(row[0]) + count * ROW_OVERHEAD_BYTES

        incoming_bytes = sum(len(item[4]) for item in incoming) + len(incoming) * ROW_OVERHEAD_BYTES
        count = int(connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
        total = logical_bytes() + incoming_bytes
        entries = count + len(incoming)
        while total > self.max_bytes or entries > self.max_entries:
            victim = connection.execute(
                "SELECT kind, key_digest, proof_digest, byte_length FROM entries "
                "ORDER BY access_sequence ASC, kind ASC, key_digest ASC, proof_digest ASC "
                "LIMIT 1"
            ).fetchone()
            if victim is None:
                raise CodeIndexError("capacity_exceeded")
            connection.execute(
                "DELETE FROM entries WHERE kind=? AND key_digest=? AND proof_digest=?",
                (str(victim[0]), str(victim[1]), str(victim[2])),
            )
            connection.execute(
                "INSERT INTO counters(kind, event, count) VALUES(?,?,1) "
                "ON CONFLICT(kind, event) DO UPDATE SET count=count+1",
                (str(victim[0]), CacheEvent.EVICTION.value),
            )
            total -= int(victim[3]) + ROW_OVERHEAD_BYTES
            entries -= 1
        if self.db_bytes() > self.max_bytes:
            raise CodeIndexError("capacity_exceeded")

    # ------------------------------------------------------------------
    # Creation and migration
    # ------------------------------------------------------------------
    def create(self, *, checkout_key: str) -> None:
        path = self.location.database
        if describe_leaf(path) != "missing":
            raise CodeIndexError("unsafe_cache")
        try:
            fd = os.open(
                path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
        except OSError:
            raise CodeIndexError("storage_unavailable") from None
        os.close(fd)
        try:
            connection = _connect(path, writable=True, max_bytes=self.max_bytes)
            try:
                connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _SCHEMA_V2.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(
                        "INSERT INTO meta(singleton, checkout_key, sequence) VALUES(1,?,0)",
                        (checkout_key,),
                    )
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
            finally:
                connection.close()
        except BaseException:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise

    def migrate_v1(self) -> None:
        """Upgrade the one recognized older layout transactionally."""

        try:
            connection = _connect(
                self.location.database, writable=True, max_bytes=self.max_bytes
            )
        except CodeIndexError:
            raise
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                application_id = int(
                    connection.execute("PRAGMA application_id").fetchone()[0]
                )
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if application_id != APPLICATION_ID:
                    raise CodeIndexError("incompatible")
                if version == SCHEMA_VERSION:
                    connection.execute("ROLLBACK")
                    return
                if version != V1_SCHEMA_VERSION or not _check_schema(
                    connection, version=V1_SCHEMA_VERSION
                ):
                    raise CodeIndexError("incompatible")
                meta = connection.execute(
                    "SELECT checkout_key FROM meta WHERE singleton=1"
                ).fetchone()
                if meta is None or str(meta[0]) != self.location.checkout_key:
                    raise CodeIndexError("incompatible")
                connection.execute(
                    "ALTER TABLE entries ADD COLUMN access_sequence INTEGER NOT NULL DEFAULT 0"
                )
                identifiers = connection.execute(
                    "SELECT kind, key_digest, proof_digest FROM entries "
                    "ORDER BY kind, key_digest, proof_digest"
                ).fetchall()
                for index, row in enumerate(identifiers, start=1):
                    connection.execute(
                        "UPDATE entries SET access_sequence=? "
                        "WHERE kind=? AND key_digest=? AND proof_digest=?",
                        (index, str(row[0]), str(row[1]), str(row[2])),
                    )
                connection.execute(
                    "CREATE INDEX idx_entries_lru "
                    "ON entries(access_sequence, kind, key_digest, proof_digest)"
                )
                connection.execute(
                    "UPDATE meta SET sequence=? WHERE singleton=1",
                    (len(identifiers),),
                )
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                if not _check_schema(connection, version=SCHEMA_VERSION):
                    raise CodeIndexError("incompatible")
                connection.execute("COMMIT")
            except BaseException:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        except sqlite3.Error:
            raise CodeIndexError("corrupt") from None
        finally:
            connection.close()

    def create_pending(self, path: Path, *, checkout_key: str) -> None:
        if describe_leaf(path) != "missing":
            raise CodeIndexError("unsafe_cache")
        try:
            fd = os.open(
                path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
        except OSError:
            raise CodeIndexError("storage_unavailable") from None
        os.close(fd)
        try:
            connection = _connect(path, writable=True, max_bytes=self.max_bytes)
            try:
                connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _SCHEMA_V2.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(
                        "INSERT INTO meta(singleton, checkout_key, sequence) VALUES(1,?,0)",
                        (checkout_key,),
                    )
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
            finally:
                connection.close()
        except BaseException:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise

    def verify_path(self, path: Path, *, checkout_key: str) -> bool:
        if describe_leaf(path) != "present":
            return False
        try:
            connection = _connect(path, writable=False, max_bytes=self.max_bytes)
        except CodeIndexError:
            return False
        try:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                return False
            if int(connection.execute("PRAGMA application_id").fetchone()[0]) != APPLICATION_ID:
                return False
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version != SCHEMA_VERSION or not _check_schema(
                connection, version=SCHEMA_VERSION
            ):
                return False
            row = connection.execute(
                "SELECT checkout_key FROM meta WHERE singleton=1"
            ).fetchone()
            return row is not None and str(row[0]) == checkout_key
        except (sqlite3.Error, ValueError, TypeError):
            return False
        finally:
            connection.close()

    def verify_published(self, *, checkout_key: str) -> bool:
        health, version, _entries, _bytes, _counts, _pending = self.inspect(deep=True)
        if health is not CacheHealth.READY or version != SCHEMA_VERSION:
            return False
        try:
            connection = _connect(
                self.location.database, writable=False, max_bytes=self.max_bytes
            )
        except CodeIndexError:
            return False
        try:
            row = connection.execute(
                "SELECT checkout_key FROM meta WHERE singleton=1"
            ).fetchone()
            return row is not None and str(row[0]) == checkout_key
        except sqlite3.Error:
            return False
        finally:
            connection.close()


def row_checksum(
    *,
    checkout_key: str,
    kind: ArtifactKind,
    key_digest: str,
    proof_digest: str,
    source_revision: str | None,
    payload: bytes,
) -> str:
    return envelope_checksum(
        checkout_key=checkout_key,
        kind=kind,
        key_digest=key_digest,
        proof_digest=proof_digest,
        source_revision=source_revision,
        payload=payload,
    )
