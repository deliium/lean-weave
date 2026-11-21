"""Validated SQLite storage for one current source generation per checkout."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable, Iterator

from lean_weave.code_index.identity import IndexLocation, describe_leaf
from lean_weave.code_index.models import (
    APPLICATION_ID,
    INDEX_SCHEMA_VERSION,
    FileRecord,
    IndexHeader,
    StoreHealth,
    expected_module_name,
    facts_sha256,
    manifest_sha256,
    source_revision,
)
from lean_weave.context.models import SymbolKind
from lean_weave.context.policy import contains_secret
from lean_weave.context.python_index import DefinitionFact, ImportFact, PythonFacts
from lean_weave.context.repository import validate_index_source_path
from lean_weave.errors import CodeIndexError
from lean_weave.logging_setup import get_logger

log = get_logger("code_index.sqlite")

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_KINDS = frozenset(item.value for item in SymbolKind)
_INDEX_OBJECTS = frozenset(
    {
        ("table", "meta"),
        ("table", "files"),
        ("table", "symbols"),
        ("table", "imports"),
        ("index", "idx_files_module"),
        ("index", "idx_files_digest"),
        ("index", "idx_symbols_simple"),
        ("index", "idx_symbols_local"),
    }
)

_SCHEMA_SQL = """
CREATE TABLE meta(
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    checkout_key TEXT NOT NULL,
    revision TEXT NOT NULL,
    policy_key TEXT NOT NULL,
    extractor_key TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL
) STRICT;
CREATE TABLE files(
    file_id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE COLLATE BINARY,
    module_name TEXT NOT NULL COLLATE BINARY,
    digest TEXT NOT NULL COLLATE BINARY,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    line_count INTEGER NOT NULL CHECK(line_count >= 0),
    parse_status TEXT NOT NULL CHECK(parse_status IN ('ok','syntax','parse_limit')),
    facts_sha256 TEXT NOT NULL
) STRICT;
CREATE TABLE symbols(
    file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    local_name TEXT NOT NULL COLLATE BINARY,
    simple_name TEXT NOT NULL COLLATE BINARY,
    kind TEXT NOT NULL CHECK(kind IN ('class','function','async_function','method','async_method')),
    parent TEXT,
    signature TEXT NOT NULL,
    decorators_json TEXT NOT NULL,
    start_line INTEGER NOT NULL CHECK(start_line >= 1),
    end_line INTEGER NOT NULL CHECK(end_line >= start_line),
    traversal_order INTEGER NOT NULL CHECK(traversal_order >= 0),
    occurrence INTEGER NOT NULL CHECK(occurrence >= 0),
    PRIMARY KEY(file_id, ordinal)
) STRICT;
CREATE TABLE imports(
    file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    kind TEXT NOT NULL CHECK(kind IN ('import','from')),
    module TEXT,
    level INTEGER NOT NULL CHECK(level >= 0),
    names_json TEXT NOT NULL,
    start_line INTEGER NOT NULL CHECK(start_line >= 1),
    end_line INTEGER NOT NULL CHECK(end_line >= start_line),
    PRIMARY KEY(file_id, ordinal)
) STRICT;
CREATE INDEX idx_files_module ON files(module_name COLLATE BINARY);
CREATE INDEX idx_files_digest ON files(digest COLLATE BINARY);
CREATE INDEX idx_symbols_simple ON symbols(simple_name COLLATE BINARY);
CREATE INDEX idx_symbols_local ON symbols(local_name COLLATE BINARY);
"""

Migration = Callable[[sqlite3.Connection], None]


def _quote(path: Path) -> str:
    return "file:" + str(path).replace("?", "%3f").replace("#", "%23")


def _connect(path: Path, *, writable: bool) -> sqlite3.Connection:
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
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA busy_timeout=0")
        if writable:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA secure_delete=ON")
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            connection.execute(
                "PRAGMA max_page_count=%d" % max(1, (64 * 1024 * 1024) // max(page_size, 1))
            )
        else:
            connection.execute("PRAGMA query_only=ON")
    except sqlite3.Error:
        connection.close()
        raise CodeIndexError("storage_unavailable") from None
    return connection


def _schema_objects(connection: sqlite3.Connection) -> set[tuple[str, str]]:
    rows = connection.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {(str(row[0]), str(row[1])) for row in rows}


def _check_schema(connection: sqlite3.Connection) -> bool:
    if _schema_objects(connection) != _INDEX_OBJECTS:
        return False
    columns = {
        "meta": ["singleton", "checkout_key", "revision", "policy_key", "extractor_key", "manifest_sha256"],
        "files": [
            "file_id", "path", "module_name", "digest", "size_bytes", "line_count",
            "parse_status", "facts_sha256",
        ],
        "symbols": [
            "file_id", "ordinal", "local_name", "simple_name", "kind", "parent", "signature",
            "decorators_json", "start_line", "end_line", "traversal_order", "occurrence",
        ],
        "imports": [
            "file_id", "ordinal", "kind", "module", "level", "names_json", "start_line", "end_line",
        ],
    }
    for table, expected in columns.items():
        actual = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]
        if actual != expected:
            return False
    return True


def _validate_name(value: str, *, allow_empty: bool = False) -> bool:
    if not isinstance(value, str):
        return False
    if not value and not allow_empty:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    return not contains_secret(value)


def _validate_record(record: FileRecord, *, checkout_key: str) -> None:
    validate_index_source_path(record.path)
    if record.module_name != expected_module_name(record.path):
        raise ValueError("module mismatch")
    if not _ID.fullmatch(record.file_id) or not _DIGEST.fullmatch(record.digest):
        raise ValueError("identity")
    if record.parse_status not in {"ok", "syntax", "parse_limit"}:
        raise ValueError("status")
    if record.parse_status != record.facts.status:
        raise ValueError("status mismatch")
    if record.line_count != record.facts.line_count:
        raise ValueError("line count")
    if record.size_bytes < 0:
        raise ValueError("size")
    for item in record.facts.definitions:
        if item.kind.value not in _KINDS:
            raise ValueError("kind")
        if not _validate_name(item.local_name) or not _validate_name(item.simple_name):
            raise ValueError("definition name")
        if item.parent is not None and not _validate_name(item.parent):
            raise ValueError("parent name")
        if not _validate_name(item.signature, allow_empty=True):
            raise ValueError("signature")
        for decorator in item.decorators:
            if not _validate_name(decorator, allow_empty=True):
                raise ValueError("decorator")
        if record.line_count == 0:
            raise ValueError("definition in empty file")
        if item.start_line < 1 or item.end_line < item.start_line or item.end_line > record.line_count:
            raise ValueError("range")
        if item.traversal_order < 0 or item.occurrence < 0:
            raise ValueError("ordinal")
    for item in record.facts.imports:
        if item.kind not in {"import", "from"}:
            raise ValueError("import kind")
        if item.module is not None and not _validate_name(item.module, allow_empty=False):
            raise ValueError("import module")
        if item.level < 0:
            raise ValueError("import level")
        for name, alias in item.names:
            if not _validate_name(name, allow_empty=False):
                raise ValueError("import name")
            if alias is not None and not _validate_name(alias, allow_empty=False):
                raise ValueError("import alias")
        if item.start_line < 1 or item.end_line < item.start_line:
            raise ValueError("import range")


def _facts_from_rows(
    file_id: str,
    status: str,
    line_count: int,
    symbol_rows: Iterable[sqlite3.Row],
    import_rows: Iterable[sqlite3.Row],
) -> PythonFacts:
    definitions: list[DefinitionFact] = []
    for row in symbol_rows:
        try:
            decorators = json.loads(row["decorators_json"])
        except (TypeError, ValueError):
            raise ValueError("decorators") from None
        if not isinstance(decorators, list) or not all(isinstance(item, str) for item in decorators):
            raise ValueError("decorators")
        definitions.append(
            DefinitionFact(
                local_name=str(row["local_name"]),
                simple_name=str(row["simple_name"]),
                kind=SymbolKind(str(row["kind"])),
                parent=None if row["parent"] is None else str(row["parent"]),
                signature=str(row["signature"]),
                decorators=tuple(decorators),
                start_line=int(row["start_line"]),
                end_line=int(row["end_line"]),
                traversal_order=int(row["traversal_order"]),
                occurrence=int(row["occurrence"]),
            )
        )
    imports: list[ImportFact] = []
    for row in import_rows:
        try:
            names = json.loads(row["names_json"])
        except (TypeError, ValueError):
            raise ValueError("import names") from None
        if not isinstance(names, list):
            raise ValueError("import names")
        pairs: list[tuple[str, str | None]] = []
        for entry in names:
            if not isinstance(entry, list) or len(entry) != 2:
                raise ValueError("import names")
            name, alias = entry
            if not isinstance(name, str) or (alias is not None and not isinstance(alias, str)):
                raise ValueError("import names")
            pairs.append((name, alias))
        imports.append(
            ImportFact(
                kind=str(row["kind"]),
                module=None if row["module"] is None else str(row["module"]),
                level=int(row["level"]),
                names=tuple(pairs),
                start_line=int(row["start_line"]),
                end_line=int(row["end_line"]),
            )
        )
    definitions.sort(key=lambda item: item.traversal_order)
    return PythonFacts(
        status=status,
        definitions=tuple(definitions),
        imports=tuple(imports),
        line_count=line_count,
    )


class SqliteIndexStore:
    """One validated generation store. Callers own the surrounding lease."""

    def __init__(
        self,
        location: IndexLocation,
        *,
        migrations: dict[int, Migration] | None = None,
    ) -> None:
        self.location = location
        self.migrations = dict(migrations or {})
        self._lock_present = describe_leaf(location.lock) == "present"

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------
    def unsafe(self) -> bool:
        leaves = self.location.leaves
        if any(describe_leaf(path) == "unsafe" for path in leaves.values()):
            return True
        if any(describe_leaf(path) == "present" for path in self.location.unsafe_sidecars):
            return True
        db = describe_leaf(self.location.database)
        pending = describe_leaf(self.location.pending)
        lock = describe_leaf(self.location.lock)
        if db == "present" and lock != "present":
            # A live database without its cooperating lock is not trusted.
            return True
        if db != "present" and pending == "present" and lock != "present":
            return False
        return False

    def requires_recovery(self) -> bool:
        return describe_leaf(self.location.database_journal) == "present"

    def inspect(self) -> tuple[StoreHealth, IndexHeader | None, tuple[FileRecord, ...]]:
        if self.unsafe():
            return StoreHealth.UNAVAILABLE, None, ()
        db_kind = describe_leaf(self.location.database)
        lock_kind = describe_leaf(self.location.lock)
        pending_kind = describe_leaf(self.location.pending)
        if db_kind != "present":
            if describe_leaf(self.location.database_journal) == "present":
                return StoreHealth.UNAVAILABLE, None, ()
            if lock_kind == "present" and pending_kind == "present":
                return StoreHealth.MISSING, None, ()
            return StoreHealth.MISSING, None, ()
        if self.requires_recovery():
            return StoreHealth.UNAVAILABLE, None, ()
        try:
            connection = _connect(self.location.database, writable=False)
        except CodeIndexError:
            return StoreHealth.UNAVAILABLE, None, ()
        try:
            connection.execute("BEGIN")
            try:
                header = self._validate_connection(connection)
                records = self._load_all(connection)
            finally:
                connection.execute("ROLLBACK")
        except CodeIndexError as exc:
            return StoreHealth(str(exc.code)) if exc.code in {"incompatible"} else StoreHealth.CORRUPT, None, ()
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            return StoreHealth.CORRUPT, None, ()
        finally:
            connection.close()
        return StoreHealth.HEALTHY, header, records

    def _validate_connection(self, connection: sqlite3.Connection) -> IndexHeader:
        result = connection.execute("PRAGMA quick_check").fetchone()
        if result is None or result[0] != "ok":
            raise CodeIndexError("corrupt")
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if application_id != APPLICATION_ID:
            raise CodeIndexError("incompatible")
        if user_version != INDEX_SCHEMA_VERSION:
            raise CodeIndexError("incompatible")
        if not _check_schema(connection):
            raise CodeIndexError("incompatible")
        row = connection.execute(
            "SELECT checkout_key, revision, policy_key, extractor_key, manifest_sha256 FROM meta WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise CodeIndexError("corrupt")
        if str(row[0]) != self.location.checkout_key:
            raise CodeIndexError("incompatible")
        counts = connection.execute(
            "SELECT (SELECT COUNT(*) FROM files), (SELECT COUNT(*) FROM symbols), (SELECT COUNT(*) FROM imports)"
        ).fetchone()
        return IndexHeader(
            schema_version=INDEX_SCHEMA_VERSION,
            checkout_key=str(row[0]),
            revision=str(row[1]),
            policy_key=str(row[2]),
            extractor_key=str(row[3]),
            manifest_sha256=str(row[4]),
            files=int(counts[0]),
            symbols=int(counts[1]),
            imports=int(counts[2]),
        )

    def _load_all(self, connection: sqlite3.Connection) -> tuple[FileRecord, ...]:
        records: list[FileRecord] = []
        files = connection.execute(
            "SELECT file_id, path, module_name, digest, size_bytes, line_count, parse_status, facts_sha256 "
            "FROM files ORDER BY path COLLATE BINARY"
        ).fetchall()
        for row in files:
            file_id = str(row["file_id"])
            symbols = connection.execute(
                "SELECT * FROM symbols WHERE file_id=? ORDER BY ordinal", (file_id,)
            ).fetchall()
            imports = connection.execute(
                "SELECT * FROM imports WHERE file_id=? ORDER BY ordinal", (file_id,)
            ).fetchall()
            facts = _facts_from_rows(
                file_id,
                str(row["parse_status"]),
                int(row["line_count"]),
                symbols,
                imports,
            )
            record = FileRecord(
                path=str(row["path"]),
                file_id=file_id,
                module_name=str(row["module_name"]),
                digest=str(row["digest"]),
                size_bytes=int(row["size_bytes"]),
                line_count=int(row["line_count"]),
                parse_status=str(row["parse_status"]),
                facts=facts,
            )
            if facts_sha256(record.facts) != str(row["facts_sha256"]):
                raise CodeIndexError("corrupt")
            _validate_record(record, checkout_key=self.location.checkout_key)
            records.append(record)
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign:
            raise CodeIndexError("corrupt")
        header_row = connection.execute(
            "SELECT checkout_key, revision, policy_key, extractor_key, manifest_sha256 FROM meta WHERE singleton=1"
        ).fetchone()
        expected = manifest_sha256(
            schema_version=INDEX_SCHEMA_VERSION,
            checkout_key=str(header_row[0]),
            revision=str(header_row[1]),
            policy_key_value=str(header_row[2]),
            extractor_key_value=str(header_row[3]),
            files=records,
        )
        if expected != str(header_row[4]):
            raise CodeIndexError("corrupt")
        expected_revision = source_revision(
            str(header_row[2]), {item.path: item.digest for item in records}
        )
        if expected_revision != str(header_row[1]):
            raise CodeIndexError("corrupt")
        return tuple(records)

    # ------------------------------------------------------------------
    # Scoped reads
    # ------------------------------------------------------------------
    @contextmanager
    def scoped_session(
        self,
        *,
        policy_key_value: str,
        extractor_key_value: str,
    ) -> Iterator[Callable[[str, str, int], FileRecord | None]]:
        """Open one read transaction and return a verified per-file loader."""

        def missing(_path: str, _digest: str, _line_count: int) -> FileRecord | None:
            return None

        if self.unsafe() or self.requires_recovery():
            yield missing
            return
        if describe_leaf(self.location.database) != "present":
            yield missing
            return
        try:
            connection = _connect(self.location.database, writable=False)
        except CodeIndexError:
            yield missing
            return
        try:
            try:
                connection.execute("BEGIN")
                header = self._validate_connection(connection)
                if (
                    header.policy_key != policy_key_value
                    or header.extractor_key != extractor_key_value
                ):
                    raise CodeIndexError("stale")
            except (sqlite3.Error, ValueError, KeyError, TypeError, CodeIndexError):
                yield missing
                return

            def load(path: str, digest: str, line_count: int) -> FileRecord | None:
                try:
                    return self._scoped_row(connection, path, digest, line_count)
                except (sqlite3.Error, ValueError, KeyError, TypeError, CodeIndexError):
                    return None

            try:
                yield load
            finally:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
        finally:
            connection.close()

    def _scoped_row(
        self,
        connection: sqlite3.Connection,
        path: str,
        digest: str,
        line_count: int,
    ) -> FileRecord | None:
        row = connection.execute(
            "SELECT file_id, path, module_name, digest, size_bytes, line_count, parse_status, facts_sha256 "
            "FROM files WHERE path=? COLLATE BINARY",
            (path,),
        ).fetchone()
        if row is None or str(row["digest"]) != digest or int(row["line_count"]) != line_count:
            return None
        file_id = str(row["file_id"])
        symbols = connection.execute(
            "SELECT * FROM symbols WHERE file_id=? ORDER BY ordinal", (file_id,)
        ).fetchall()
        imports = connection.execute(
            "SELECT * FROM imports WHERE file_id=? ORDER BY ordinal", (file_id,)
        ).fetchall()
        facts = _facts_from_rows(
            file_id,
            str(row["parse_status"]),
            int(row["line_count"]),
            symbols,
            imports,
        )
        if facts_sha256(facts) != str(row["facts_sha256"]):
            return None
        record = FileRecord(
            path=str(row["path"]),
            file_id=file_id,
            module_name=str(row["module_name"]),
            digest=str(row["digest"]),
            size_bytes=int(row["size_bytes"]),
            line_count=int(row["line_count"]),
            parse_status=str(row["parse_status"]),
            facts=facts,
        )
        _validate_record(record, checkout_key=self.location.checkout_key)
        return record

    def scoped_file(
        self,
        *,
        path: str,
        digest: str,
        line_count: int,
        policy_key_value: str,
        extractor_key_value: str,
    ) -> FileRecord | None:
        """Load one verified path-independent fact record without a manifest scan."""

        with self.scoped_session(
            policy_key_value=policy_key_value,
            extractor_key_value=extractor_key_value,
        ) as load:
            return load(path, digest, line_count)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def write_generation(
        self,
        *,
        revision: str,
        policy_key_value: str,
        extractor_key_value: str,
        files: Iterable[FileRecord],
    ) -> IndexHeader:
        records = tuple(sorted(files, key=lambda item: item.path))
        for record in records:
            _validate_record(record, checkout_key=self.location.checkout_key)
        checksum = manifest_sha256(
            schema_version=INDEX_SCHEMA_VERSION,
            checkout_key=self.location.checkout_key,
            revision=revision,
            policy_key_value=policy_key_value,
            extractor_key_value=extractor_key_value,
            files=records,
        )
        try:
            connection = _connect(self.location.database, writable=True)
        except CodeIndexError:
            raise
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._validate_connection(connection)
                self._replace_rows(
                    connection,
                    revision=revision,
                    policy_key_value=policy_key_value,
                    extractor_key_value=extractor_key_value,
                    checksum=checksum,
                    records=records,
                )
                self._assert_integrity(connection, records=records, checksum=checksum)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        except sqlite3.Error:
            raise CodeIndexError("storage_unavailable") from None
        except ValueError:
            raise CodeIndexError("unsafe_facts") from None
        finally:
            connection.close()
        return IndexHeader(
            schema_version=INDEX_SCHEMA_VERSION,
            checkout_key=self.location.checkout_key,
            revision=revision,
            policy_key=policy_key_value,
            extractor_key=extractor_key_value,
            manifest_sha256=checksum,
            files=len(records),
            symbols=sum(len(item.facts.definitions) for item in records),
            imports=sum(len(item.facts.imports) for item in records),
        )

    def create_pending(
        self,
        path: Path,
        *,
        revision: str,
        policy_key_value: str,
        extractor_key_value: str,
        files: Iterable[FileRecord],
    ) -> IndexHeader:
        records = tuple(sorted(files, key=lambda item: item.path))
        for record in records:
            _validate_record(record, checkout_key=self.location.checkout_key)
        checksum = manifest_sha256(
            schema_version=INDEX_SCHEMA_VERSION,
            checkout_key=self.location.checkout_key,
            revision=revision,
            policy_key_value=policy_key_value,
            extractor_key_value=extractor_key_value,
            files=records,
        )
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
            connection = _connect(path, writable=True)
            try:
                connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version={INDEX_SCHEMA_VERSION}")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _SCHEMA_SQL.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    self._insert_rows(
                        connection,
                        revision=revision,
                        policy_key_value=policy_key_value,
                        extractor_key_value=extractor_key_value,
                        checksum=checksum,
                        records=records,
                    )
                    self._assert_integrity(connection, records=records, checksum=checksum)
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
            finally:
                connection.close()
        except BaseException:
            with _suppress_os():
                os.unlink(path)
            raise
        return IndexHeader(
            schema_version=INDEX_SCHEMA_VERSION,
            checkout_key=self.location.checkout_key,
            revision=revision,
            policy_key=policy_key_value,
            extractor_key=extractor_key_value,
            manifest_sha256=checksum,
            files=len(records),
            symbols=sum(len(item.facts.definitions) for item in records),
            imports=sum(len(item.facts.imports) for item in records),
        )

    def _replace_rows(
        self,
        connection: sqlite3.Connection,
        *,
        revision: str,
        policy_key_value: str,
        extractor_key_value: str,
        checksum: str,
        records: tuple[FileRecord, ...],
    ) -> None:
        connection.execute("DELETE FROM files")
        self._insert_rows(
            connection,
            revision=revision,
            policy_key_value=policy_key_value,
            extractor_key_value=extractor_key_value,
            checksum=checksum,
            records=records,
        )

    def _insert_rows(
        self,
        connection: sqlite3.Connection,
        *,
        revision: str,
        policy_key_value: str,
        extractor_key_value: str,
        checksum: str,
        records: tuple[FileRecord, ...],
    ) -> None:
        connection.execute("DELETE FROM meta")
        connection.execute(
            "INSERT INTO meta(singleton, checkout_key, revision, policy_key, extractor_key, manifest_sha256) "
            "VALUES(1,?,?,?,?,?)",
            (
                self.location.checkout_key,
                revision,
                policy_key_value,
                extractor_key_value,
                checksum,
            ),
        )
        for record in records:
            connection.execute(
                "INSERT INTO files(file_id, path, module_name, digest, size_bytes, line_count, parse_status, facts_sha256) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    record.file_id,
                    record.path,
                    record.module_name,
                    record.digest,
                    record.size_bytes,
                    record.line_count,
                    record.parse_status,
                    facts_sha256(record.facts),
                ),
            )
            definitions = sorted(
                record.facts.definitions,
                key=lambda item: (item.start_line, item.end_line, item.traversal_order),
            )
            for ordinal, item in enumerate(definitions):
                connection.execute(
                    "INSERT INTO symbols(file_id, ordinal, local_name, simple_name, kind, parent, signature, "
                    "decorators_json, start_line, end_line, traversal_order, occurrence) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        record.file_id,
                        ordinal,
                        item.local_name,
                        item.simple_name,
                        item.kind.value,
                        item.parent,
                        item.signature,
                        json.dumps(list(item.decorators), ensure_ascii=False, separators=(",", ":")),
                        item.start_line,
                        item.end_line,
                        item.traversal_order,
                        item.occurrence,
                    ),
                )
            ordered_imports = sorted(
                record.facts.imports,
                key=lambda item: (item.start_line, item.end_line, item.module or ""),
            )
            for ordinal, item in enumerate(ordered_imports):
                connection.execute(
                    "INSERT INTO imports(file_id, ordinal, kind, module, level, names_json, start_line, end_line) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        record.file_id,
                        ordinal,
                        item.kind,
                        item.module,
                        item.level,
                        json.dumps(
                            [[name, alias] for name, alias in item.names],
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        item.start_line,
                        item.end_line,
                    ),
                )

    def _assert_integrity(
        self,
        connection: sqlite3.Connection,
        *,
        records: tuple[FileRecord, ...],
        checksum: str,
    ) -> None:
        result = connection.execute("PRAGMA quick_check").fetchone()
        if result is None or result[0] != "ok":
            raise CodeIndexError("corrupt")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise CodeIndexError("corrupt")
        header_row = connection.execute(
            "SELECT checkout_key, revision, policy_key, extractor_key, manifest_sha256 FROM meta WHERE singleton=1"
        ).fetchone()
        if header_row is None or str(header_row[4]) != checksum:
            raise CodeIndexError("corrupt")
        recomputed = manifest_sha256(
            schema_version=INDEX_SCHEMA_VERSION,
            checkout_key=str(header_row[0]),
            revision=str(header_row[1]),
            policy_key_value=str(header_row[2]),
            extractor_key_value=str(header_row[3]),
            files=records,
        )
        if recomputed != checksum:
            raise CodeIndexError("corrupt")

    # ------------------------------------------------------------------
    # Migration registry (no historical production schema in this release)
    # ------------------------------------------------------------------
    def migrate(self) -> None:
        """Apply registered migrations from the observed version to current."""

        if describe_leaf(self.location.database) != "present":
            raise CodeIndexError("missing")
        try:
            reader = _connect(self.location.database, writable=False)
            try:
                version = int(reader.execute("PRAGMA user_version").fetchone()[0])
                application_id = int(reader.execute("PRAGMA application_id").fetchone()[0])
            finally:
                reader.close()
        except sqlite3.Error:
            raise CodeIndexError("corrupt") from None
        if application_id != APPLICATION_ID:
            raise CodeIndexError("incompatible")
        if version == INDEX_SCHEMA_VERSION:
            return
        chain: list[int] = []
        current = version
        while current != INDEX_SCHEMA_VERSION:
            if current not in self.migrations:
                raise CodeIndexError("incompatible")
            chain.append(current)
            current += 1
            if len(chain) > 32:
                raise CodeIndexError("incompatible")
        try:
            connection = _connect(self.location.database, writable=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for version_key in chain:
                        self.migrations[version_key](connection)
                    connection.execute(f"PRAGMA user_version={INDEX_SCHEMA_VERSION}")
                    self._assert_integrity(
                        connection,
                        records=self._load_all(connection),
                        checksum=str(
                            connection.execute("SELECT manifest_sha256 FROM meta WHERE singleton=1").fetchone()[0]
                        ),
                    )
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
            finally:
                connection.close()
        except sqlite3.Error:
            raise CodeIndexError("corrupt") from None


class _suppress_os:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, OSError)
