from __future__ import annotations

import json
import multiprocessing
import sqlite3
from pathlib import Path

import pytest

from lean_weave.config.models import ContextCacheConfig, ContextConfig
from lean_weave.context_cache import ArtifactKind, CacheKey, CacheProof
from lean_weave.context_cache.service import SqliteContextCache


class JsonCodec:
    kind = ArtifactKind.FACTS

    def __init__(self, kind: ArtifactKind = ArtifactKind.FACTS) -> None:
        self.kind = kind

    def encode(self, value):
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

    def decode(self, payload):
        return json.loads(payload.decode())

    def validate(self, value, proof):
        return isinstance(value, dict)


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SqliteContextCache:
    root = tmp_path / "repo"
    root.mkdir()
    cache_base = tmp_path / "cache"
    cache_base.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_base))
    return SqliteContextCache(
        root,
        config=ContextCacheConfig(enabled=True, max_entries=64),
        context_config=ContextConfig(),
        cache_base=cache_base,
    )


def _key(name: str) -> CacheKey:
    return CacheKey.create(ArtifactKind.FACTS, inputs={"name": name})


def _proof(name: str) -> CacheProof:
    return CacheProof.create(
        ArtifactKind.FACTS, source_revision="sha256:" + "a" * 64, inputs={"name": name}
    )


def test_roundtrip_and_miss(cache: SqliteContextCache):
    codec = JsonCodec()
    missing = cache.lookup(_key("a"), _proof("a"), codec=codec)
    assert missing.result.value == "miss"

    assert cache.publish(_key("a"), _proof("a"), {"v": 1}, codec=codec) is True
    hit = cache.lookup(_key("a"), _proof("a"), codec=codec)
    assert hit.result.value == "hit"
    assert hit.value == {"v": 1}

    other = cache.lookup(_key("a"), _proof("b"), codec=codec)
    assert other.result.value == "invalidated"
    assert other.reason.value == "stale_proof"


def test_status_and_stats_are_read_only(cache: SqliteContextCache):
    assert not cache.status().db_bytes
    assert cache.status().health.value == "missing"
    assert cache.stats().health.value == "missing"
    assert cache.status().entries is None or cache.status().entries == 0
    assert cache.lookup(_key("a"), _proof("a"), codec=JsonCodec()).result.value == "miss"
    assert cache.status().health.value == "missing"

    cache.publish(_key("a"), _proof("a"), {"v": 1}, codec=JsonCodec())
    status = cache.status()
    assert status.health.value == "ready"
    assert status.entries == 1
    assert status.schema_version == 2
    stats = cache.stats()
    assert stats.count("hit") >= 0
    assert stats.count("miss") >= 1


def test_corrupt_checksum_is_invalidated(cache: SqliteContextCache):
    cache.publish(_key("a"), _proof("a"), {"v": 1}, codec=JsonCodec())
    database = next((Path(cache.cache_base)).rglob("cache.sqlite3"))
    connection = sqlite3.connect(database)
    connection.execute("UPDATE entries SET envelope_checksum=? ", ("sha256:" + "0" * 64,))
    connection.commit()
    connection.close()
    outcome = cache.lookup(_key("a"), _proof("a"), codec=JsonCodec())
    assert outcome.result.value == "invalidated"
    assert outcome.reason.value == "checksum_mismatch"


def test_corrupt_shape_and_length(cache: SqliteContextCache):
    cache.publish(_key("a"), _proof("a"), {"v": 1}, codec=JsonCodec())
    database = next(Path(cache.cache_base).rglob("cache.sqlite3"))
    connection = sqlite3.connect(database)
    connection.execute("UPDATE entries SET payload=?", (b"not json",))
    connection.commit()
    connection.close()
    outcome = cache.lookup(_key("a"), _proof("a"), codec=JsonCodec())
    assert outcome.result.value == "invalidated"


def test_schema_mismatch_is_bypass(cache: SqliteContextCache):
    cache.publish(_key("a"), _proof("a"), {"v": 1}, codec=JsonCodec())
    database = next(Path(cache.cache_base).rglob("cache.sqlite3"))
    connection = sqlite3.connect(database)
    connection.execute("DROP INDEX idx_entries_lru")
    connection.commit()
    connection.close()
    outcome = cache.lookup(_key("a"), _proof("a"), codec=JsonCodec())
    assert outcome.result.value == "bypass"


def test_lru_eviction_and_capacity(tmp_path: Path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    base = tmp_path / "cache"
    base.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CACHE_HOME", str(base))
    cache = SqliteContextCache(
        root,
        config=ContextCacheConfig(enabled=True, max_entries=2),
        context_config=ContextConfig(),
        cache_base=base,
    )
    for name in ("a", "b"):
        assert cache.publish(_key(name), _proof(name), {"v": name}, codec=JsonCodec())
    assert cache.lookup(_key("a"), _proof("a"), codec=JsonCodec()).hit
    assert cache.publish(_key("c"), _proof("c"), {"v": "c"}, codec=JsonCodec())
    assert cache.lookup(_key("b"), _proof("b"), codec=JsonCodec()).result.value == "miss"
    assert cache.lookup(_key("a"), _proof("a"), codec=JsonCodec()).hit
    assert cache.status().entries == 2


def test_oversize_payload_is_refused(cache: SqliteContextCache):
    class BigCodec(JsonCodec):
        def encode(self, value):
            return b"x" * (256 * 1024 + 1)

    assert cache.publish(_key("a"), _proof("a"), {"v": 1}, codec=BigCodec()) is False
    assert cache.status().health.value == "missing"


def test_clear_and_epoch_fence(cache: SqliteContextCache):
    cache.publish(_key("a"), _proof("a"), {"v": 1}, codec=JsonCodec())
    batch = cache.begin_batch()
    assert batch.usable
    assert batch.stage(_key("b"), _proof("b"), JsonCodec().encode({"v": 2}), kind=ArtifactKind.FACTS)
    assert cache.clear() is True
    assert cache.flush(batch) is False
    assert cache.lookup(_key("b"), _proof("b"), codec=JsonCodec()).result.value in {
        "miss",
        "bypass",
    }


def test_migration_from_v1(cache: SqliteContextCache):
    cache.publish(_key("a"), _proof("a"), {"v": 1}, codec=JsonCodec())
    database = next(Path(cache.cache_base).rglob("cache.sqlite3"))
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version=1")
    connection.execute("DROP INDEX idx_entries_lru")
    connection.execute("ALTER TABLE entries DROP COLUMN access_sequence")
    connection.commit()
    connection.close()

    status = cache.status()
    assert status.health.value == "ready"
    assert status.schema_version == 1
    assert status.migration_pending is True

    assert cache.publish(_key("b"), _proof("b"), {"v": 2}, codec=JsonCodec()) is True
    migrated = cache.status()
    assert migrated.schema_version == 2
    assert migrated.migration_pending is False
    assert cache.lookup(_key("a"), _proof("a"), codec=JsonCodec()).hit


def test_unknown_schema_is_not_overwritten(cache: SqliteContextCache):
    cache.publish(_key("a"), _proof("a"), {"v": 1}, codec=JsonCodec())
    database = next(Path(cache.cache_base).rglob("cache.sqlite3"))
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version=99")
    connection.commit()
    connection.close()

    assert cache.status().health.value == "incompatible"
    assert cache.publish(_key("b"), _proof("b"), {"v": 2}, codec=JsonCodec()) is False
    connection = sqlite3.connect(database)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
    connection.close()


def test_rebuild_and_failed_seed_keeps_old_generation(cache: SqliteContextCache):
    cache.publish(_key("old"), _proof("old"), {"v": 1}, codec=JsonCodec())

    def seed(writer):
        writer.publish(_key("new"), _proof("new"), {"v": 2}, codec=JsonCodec())

    status = cache.rebuild(seed, revalidate=lambda: True)
    assert status.health.value == "ready"
    assert cache.lookup(_key("new"), _proof("new"), codec=JsonCodec()).hit
    assert cache.lookup(_key("old"), _proof("old"), codec=JsonCodec()).result.value == "miss"

    def bad_seed(writer):
        writer.publish(_key("bad"), _proof("bad"), {"v": 3}, codec=JsonCodec())
        raise RuntimeError("seed failed")

    with pytest.raises(RuntimeError):
        cache.rebuild(bad_seed, revalidate=lambda: True)
    assert cache.lookup(_key("new"), _proof("new"), codec=JsonCodec()).hit
    assert cache.lookup(_key("bad"), _proof("bad"), codec=JsonCodec()).result.value == "miss"

    with pytest.raises(Exception):
        cache.rebuild(seed, revalidate=lambda: False)


def _worker(cache_root: str, root: str, barrier, start: int) -> None:
    import os

    os.environ["XDG_CACHE_HOME"] = cache_root
    cache = SqliteContextCache(
        Path(root),
        config=ContextCacheConfig(enabled=True, max_entries=256),
        context_config=ContextConfig(),
        cache_base=Path(cache_root),
    )
    barrier.wait()
    for index in range(10):
        name = f"{start}-{index}"
        assert cache.publish(_key(name), _proof(name), {"v": name}, codec=JsonCodec())


def test_concurrent_processes_do_not_lose_writes(cache: SqliteContextCache):
    barrier = multiprocessing.Barrier(2)
    processes = [
        multiprocessing.Process(
            target=_worker,
            args=(str(cache.cache_base), str(cache.root), barrier, index),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=60)
        assert process.exitcode == 0
    for start in range(2):
        for index in range(10):
            name = f"{start}-{index}"
            assert cache.lookup(_key(name), _proof(name), codec=JsonCodec()).hit
