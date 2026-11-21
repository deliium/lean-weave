from __future__ import annotations

import fcntl
import os
from pathlib import Path

import pytest

from lean_weave.code_index import CodeIndex
from lean_weave.code_index.identity import (
    ensure_location,
    index_location,
    open_lock,
    resolve_checkout,
)
from lean_weave.errors import CodeIndexError


def test_checkout_identity_is_path_and_inode_bound(checkout: Path, tmp_path: Path):
    one = resolve_checkout(checkout)
    same = resolve_checkout(checkout / "pkg" / "..")
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = resolve_checkout(other_root)
    assert one.key == same.key
    assert one.key != other.key


def test_cache_location_is_fixed_and_side_effect_free(checkout: Path, cache: Path):
    location = index_location(resolve_checkout(checkout), cache_base=cache)
    assert location.checkout_dir == cache / "lean-weave" / "code-index" / location.checkout_key
    assert not location.checkout_dir.exists()
    CodeIndex(checkout, cache_base=cache).status()
    assert not location.checkout_dir.exists()


def test_relative_xdg_cache_is_refused(checkout: Path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", "relative/cache")
    with pytest.raises(CodeIndexError) as exc:
        CodeIndex(checkout)._location()
    assert exc.value.code == "unsafe_cache"


def test_cache_inside_source_is_refused(checkout: Path):
    with pytest.raises(CodeIndexError) as exc:
        CodeIndex(checkout, cache_base=checkout / ".cache")._location()
    assert exc.value.code == "unsafe_cache"


def test_existing_public_cache_directory_is_refused(checkout: Path, cache: Path):
    public = cache / "lean-weave"
    public.mkdir(mode=0o755)
    os.chmod(public, 0o755)
    with pytest.raises(CodeIndexError) as exc:
        CodeIndex(checkout, cache_base=cache).build()
    assert exc.value.code == "unsafe_cache"


def test_symlinked_cache_component_is_refused(checkout: Path, cache: Path, tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    (cache / "lean-weave").symlink_to(target)
    with pytest.raises(CodeIndexError) as exc:
        CodeIndex(checkout, cache_base=cache).build()
    assert exc.value.code == "unsafe_cache"


def test_missing_read_leaves_filesystem_untouched(checkout: Path, cache: Path):
    before = sorted(str(item) for item in cache.rglob("*"))
    status = CodeIndex(checkout, cache_base=cache).status()
    assert status.health.value == "missing"
    assert sorted(str(item) for item in cache.rglob("*")) == before


def test_contended_write_lease_reports_busy(checkout: Path, cache: Path):
    index = CodeIndex(checkout, cache_base=cache)
    location = index._location()
    ensure_location(location)
    fd = open_lock(location.lock, create=True)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(CodeIndexError) as exc:
            index.build()
        assert exc.value.code == "busy"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
