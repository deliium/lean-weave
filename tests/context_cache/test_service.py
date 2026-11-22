from __future__ import annotations

import os
from pathlib import Path

import pytest

from lean_weave.config.models import ContextCacheConfig, ContextConfig
from lean_weave.context_cache.service import SqliteContextCache


@pytest.fixture
def cache(tmp_path: Path, monkeypatch) -> SqliteContextCache:
    root = tmp_path / "repo"
    root.mkdir()
    base = tmp_path / "cache"
    base.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CACHE_HOME", str(base))
    return SqliteContextCache(
        root,
        config=ContextCacheConfig(enabled=True),
        context_config=ContextConfig(),
        cache_base=base,
    )


def test_status_and_stats_do_not_create_storage(cache: SqliteContextCache):
    assert cache.status().health.value == "missing"
    assert cache.stats().health.value == "missing"
    assert not (cache.cache_base / "lean-weave").exists()


def test_competing_reader_reports_busy(cache: SqliteContextCache):
    with cache._exclusive_lease():
        assert cache.status().health.value == "busy"


def test_unsafe_leaf_is_reported(cache: SqliteContextCache):
    with cache._exclusive_lease() as fd:
        from lean_weave.context_cache.store import new_epoch, write_epoch

        write_epoch(fd, new_epoch())
    location = cache._location()
    target = location.checkout_dir / "elsewhere"
    target.mkdir()
    os.symlink(target, location.database)
    assert cache.status().health.value == "unsafe"


def test_clear_retains_lock_and_advances_epoch(cache: SqliteContextCache):
    assert cache.clear() is False
    location = cache._location()
    assert location.lock.exists()
    first = location.lock.read_bytes()
    assert cache.clear() is False
    second = location.lock.read_bytes()
    assert first != second


def test_rebuild_refuses_hot_journal(cache: SqliteContextCache):
    from lean_weave.errors import CodeIndexError

    assert cache.clear() is False
    location = cache._location()
    location.database_journal.write_bytes(b"hot")
    with pytest.raises(CodeIndexError, match="recovery_required"):
        cache.rebuild(lambda writer: None, revalidate=lambda: True)
