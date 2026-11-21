from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from lean_weave.code_index import CodeIndex
from lean_weave.code_index.sqlite_store import SqliteIndexStore
from lean_weave.errors import CodeIndexError


def _db(index: CodeIndex) -> Path:
    return index._location().database


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_build_and_reopen_are_healthy(index: CodeIndex):
    update = index.build()
    assert update.schema_version == 1
    status = index.status()
    assert status.health.value == "healthy"
    assert status.freshness.value == "current"
    assert status.files == 3
    assert status.symbols >= 2
    assert _db(index).exists()


def test_reads_never_create_storage(index: CodeIndex):
    index.status()
    assert not _db(index).exists()
    with pytest.raises(CodeIndexError) as exc:
        index.update()
    assert exc.value.code == "missing"


def test_changed_meta_is_corrupt(index: CodeIndex):
    index.build()
    connection = sqlite3.connect(_db(index))
    connection.execute("UPDATE meta SET revision='sha256:" + "0" * 64 + "'")
    connection.commit()
    connection.close()
    assert index.status().health.value == "corrupt"


def test_truncated_file_is_corrupt(index: CodeIndex):
    index.build()
    path = _db(index)
    path.write_bytes(path.read_bytes()[:64])
    assert index.status().health.value == "corrupt"


def test_wrong_application_and_version(index: CodeIndex):
    index.build()
    connection = sqlite3.connect(_db(index))
    connection.execute("PRAGMA application_id=1")
    connection.commit()
    connection.close()
    assert index.status().health.value == "incompatible"


def test_foreign_schema_object_is_incompatible(index: CodeIndex):
    index.build()
    connection = sqlite3.connect(_db(index))
    connection.execute("CREATE TRIGGER forged AFTER INSERT ON files BEGIN SELECT 1; END")
    connection.commit()
    connection.close()
    assert index.status().health.value == "incompatible"


def test_broken_foreign_key_is_corrupt(index: CodeIndex):
    index.build()
    connection = sqlite3.connect(_db(index))
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(
        "INSERT INTO symbols(file_id, ordinal, local_name, simple_name, kind, parent, signature, "
        "decorators_json, start_line, end_line, traversal_order, occurrence) "
        "VALUES('sha256:" + "0" * 64 + "', 0, 'x', 'x', 'function', NULL, 'def x()', '[]', 1, 1, 0, 0)"
    )
    connection.commit()
    connection.close()
    assert index.status().health.value == "corrupt"


def test_unexpected_wal_is_unavailable(index: CodeIndex):
    index.build()
    (_db(index).parent / "index.sqlite3-wal").write_bytes(b"")
    assert index.status().health.value == "unavailable"


def test_rollback_journal_requires_recovery(index: CodeIndex):
    index.build()
    journal = _db(index).parent / "index.sqlite3-journal"
    journal.write_bytes(b"live")
    assert index.status().health.value == "unavailable"
    with pytest.raises(CodeIndexError) as exc:
        index.build()
    assert exc.value.code == "recovery_required"
    assert journal.exists()
    assert index.clear() is True
    assert not journal.exists()
    assert index.build().schema_version == 1


def test_rejected_update_leaves_bytes_identical(index: CodeIndex):
    index.build()
    connection = sqlite3.connect(_db(index))
    connection.execute("UPDATE meta SET revision='sha256:" + "0" * 64 + "'")
    connection.commit()
    connection.close()
    before = _digest(_db(index))
    with pytest.raises(CodeIndexError) as exc:
        index.update()
    assert exc.value.code == "corrupt"
    assert _digest(_db(index)) == before


def test_checksum_mutation_with_valid_sql_is_corrupt(index: CodeIndex):
    index.build()
    connection = sqlite3.connect(_db(index))
    connection.execute("UPDATE files SET line_count=line_count+1 WHERE path='pkg/core.py'")
    connection.commit()
    connection.close()
    assert index.status().health.value == "corrupt"


def test_registered_migration_is_all_or_nothing(index: CodeIndex):
    index.build()
    connection = sqlite3.connect(_db(index))
    connection.execute("PRAGMA user_version=0")
    connection.commit()
    connection.close()
    store = SqliteIndexStore(index._location(), migrations={0: lambda conn: None})
    store.migrate()
    assert index.status().health.value == "healthy"


def test_unregistered_migration_is_rejected(index: CodeIndex):
    index.build()
    connection = sqlite3.connect(_db(index))
    connection.execute("PRAGMA user_version=0")
    connection.commit()
    connection.close()
    before = _digest(_db(index))
    with pytest.raises(CodeIndexError) as exc:
        SqliteIndexStore(index._location()).migrate()
    assert exc.value.code == "incompatible"
    assert _digest(_db(index)) == before
