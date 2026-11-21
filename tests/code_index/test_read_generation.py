from __future__ import annotations

import contextlib
import fcntl
import os
import sqlite3
from pathlib import Path

import pytest

import lean_weave.code_index.index as index_module
from lean_weave.code_index import CodeIndex, IndexGeneration
from lean_weave.code_index.identity import ensure_location, open_lock
from lean_weave.config.models import ContextConfig
from lean_weave.errors import CodeIndexError


def test_healthy_generation_is_ordered_immutable(checkout: Path, cache: Path):
    (checkout / "pkg" / "types.pyi").write_text("class Stub: ...\n", encoding="utf-8")
    index = CodeIndex(checkout, cache_base=cache)
    index.build()
    before = sorted(str(item) for item in cache.rglob("*"))
    generation = index.read_generation()
    assert isinstance(generation, IndexGeneration)
    assert [item.path for item in generation.records] == sorted(
        item.path for item in generation.records
    )
    assert "pkg/types.pyi" in {item.path for item in generation.records}
    broken = next(item for item in generation.records if item.path == "pkg/broken.py")
    assert broken.parse_status == "syntax"
    assert broken.facts.definitions == ()
    assert generation.records is generation.records
    with pytest.raises(AttributeError):
        generation.records = ()  # type: ignore[misc]
    assert sorted(str(item) for item in cache.rglob("*")) == before


def test_empty_generation_is_valid(tmp_path: Path, cache: Path):
    root = tmp_path / "empty"
    root.mkdir()
    index = CodeIndex(root, cache_base=cache)
    index.build()
    generation = index.read_generation()
    assert generation.records == ()
    assert generation.header.files == 0


def test_read_generation_never_parses(index: CodeIndex, monkeypatch):
    index.build()

    def fail(_snapshot):
        raise AssertionError("read_generation must not parse")

    monkeypatch.setattr(index_module, "extract_python_facts", fail)
    generation = index.read_generation()
    assert len(generation.records) == 3
    assert index.lookup("helper").hits


def test_stale_edit_refuses_generation(index: CodeIndex, checkout: Path):
    index.build()
    (checkout / "pkg" / "core.py").write_text("def changed():\n    return 1\n")
    with pytest.raises(CodeIndexError) as exc:
        index.read_generation()
    assert exc.value.code == "stale"
    with pytest.raises(CodeIndexError) as lookup_exc:
        index.lookup("helper")
    assert lookup_exc.value.code == "stale"


def test_extractor_drift_refuses_generation(index: CodeIndex, monkeypatch):
    index.build()
    monkeypatch.setattr(index_module, "extractor_key", lambda: "alt-extractor")
    with pytest.raises(CodeIndexError) as exc:
        index.read_generation()
    assert exc.value.code == "stale"
    with pytest.raises(CodeIndexError) as lookup_exc:
        index.lookup("helper")
    assert lookup_exc.value.code == "stale"


def test_policy_drift_refuses_generation(checkout: Path, cache: Path):
    index = CodeIndex(checkout, cache_base=cache)
    index.build()
    drifted = CodeIndex(
        checkout,
        cache_base=cache,
        context_config=ContextConfig(exclusions=["*.bak"]),
    )
    with pytest.raises(CodeIndexError) as exc:
        drifted.read_generation()
    assert exc.value.code == "stale"


def test_busy_lease_refuses_generation(checkout: Path, cache: Path):
    index = CodeIndex(checkout, cache_base=cache)
    index.build()
    location = index._location()
    ensure_location(location)
    fd = open_lock(location.lock, create=True)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(CodeIndexError) as exc:
            index.read_generation()
        assert exc.value.code == "busy"
        with pytest.raises(CodeIndexError) as lookup_exc:
            index.lookup("helper")
        assert lookup_exc.value.code == "busy"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_corrupt_store_refuses_generation(index: CodeIndex):
    index.build()
    connection = sqlite3.connect(index._location().database)
    connection.execute(
        "UPDATE files SET digest='sha256:" + "0" * 64 + "' WHERE path='pkg/core.py'"
    )
    connection.commit()
    connection.close()
    with pytest.raises(CodeIndexError) as exc:
        index.read_generation()
    assert exc.value.code == "corrupt"
    with pytest.raises(CodeIndexError) as lookup_exc:
        index.lookup("helper")
    assert lookup_exc.value.code == "corrupt"


def test_incomplete_scan_refuses_generation(index: CodeIndex, checkout: Path):
    index.build()
    # A symlinked .gitignore makes the strict scan incomplete (indeterminate).
    target = checkout / "ignore-target"
    target.write_text("*\n")
    (checkout / ".gitignore").symlink_to(target)
    with pytest.raises(CodeIndexError) as exc:
        index.read_generation()
    assert exc.value.code == "incomplete"
    with pytest.raises(CodeIndexError) as lookup_exc:
        index.lookup("helper")
    assert lookup_exc.value.code == "incomplete"


def test_final_witness_failure_refuses_generation(index: CodeIndex, monkeypatch):
    index.build()
    original = index_module.Repository.scan_python_sources

    @contextlib.contextmanager
    def wrapped(self):
        with original(self) as scan:

            class _Proxy:
                def __getattr__(self, name: str):
                    return getattr(scan, name)

                def revalidate(self) -> bool:
                    return False

            yield _Proxy()

    monkeypatch.setattr(index_module.Repository, "scan_python_sources", wrapped)
    with pytest.raises(CodeIndexError) as exc:
        index.read_generation()
    assert exc.value.code == "storage_unavailable"
    with pytest.raises(CodeIndexError) as lookup_exc:
        index.lookup("helper")
    assert lookup_exc.value.code == "storage_unavailable"


def test_missing_index_refuses_generation(index: CodeIndex):
    with pytest.raises(CodeIndexError) as exc:
        index.read_generation()
    assert exc.value.code == "missing"
