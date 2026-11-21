from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import lean_weave.code_index.index as index_module
from lean_weave.code_index import CodeIndex
from lean_weave.errors import CodeIndexError, InvalidContextRequest


def test_lookup_exact_case_and_duplicates(checkout: Path, cache: Path):
    (checkout / "pkg" / "other.py").write_text("def helper(x):\n    return x\n")
    index = CodeIndex(checkout, cache_base=cache)
    index.build()
    hits = index.lookup("helper").hits
    assert [item.path for item in hits] == ["pkg/core.py", "pkg/other.py"]
    assert index.lookup("Helper").hits == ()


def test_lookup_module_and_qualified_names(checkout: Path, cache: Path):
    index = CodeIndex(checkout, cache_base=cache)
    index.build()
    module = index.lookup("pkg.core").hits
    assert len(module) == 1
    assert module[0].kind == "module"
    assert (module[0].start_line, module[0].end_line) == (1, 8)
    qualified = index.lookup("pkg.core.Widget.render").hits
    assert qualified[0].simple_name == "render"
    local = index.lookup("Widget.render").hits
    assert local[0].name == "pkg.core.Widget.render"


def test_empty_module_has_nullable_range(checkout: Path, cache: Path):
    index = CodeIndex(checkout, cache_base=cache)
    index.build()
    hit = index.lookup("pkg").hits[0]
    assert hit.kind == "module"
    assert hit.start_line is None and hit.end_line is None


def test_lookup_limit_and_stable_identifier(checkout: Path, cache: Path):
    (checkout / "pkg" / "other.py").write_text("def helper(x):\n    return x\n")
    index = CodeIndex(checkout, cache_base=cache)
    index.build()
    all_hits = index.lookup("helper").hits
    limited = index.lookup("helper", limit=1)
    assert limited.truncated is True
    assert limited.hits[0].symbol_id == all_hits[0].symbol_id


def test_invalid_selector_is_rejected_before_io(index: CodeIndex, monkeypatch):
    def fail():
        raise AssertionError("location must not resolve")

    monkeypatch.setattr(CodeIndex, "_location", lambda self: fail())
    for value in ("", "  ", "bad\x00name"):
        with pytest.raises(InvalidContextRequest):
            index.lookup(value)
    with pytest.raises(InvalidContextRequest):
        index.lookup("helper", limit=True)


def test_stale_generation_refuses_lookup(index: CodeIndex, checkout: Path):
    index.build()
    (checkout / "pkg" / "core.py").write_text("def changed():\n    return 1\n")
    with pytest.raises(CodeIndexError) as exc:
        index.lookup("helper")
    assert exc.value.code == "stale"
    assert index.status().freshness.value == "stale"


def test_absent_path_returns_zero_when_current(index: CodeIndex):
    index.build()
    result = index.lookup("helper", path="pkg/missing.py")
    assert result.hits == ()
    assert result.truncated is False


def test_corrupt_rows_are_never_hits(index: CodeIndex):
    index.build()
    connection = sqlite3.connect(index._location().database)
    connection.execute("UPDATE files SET digest='sha256:" + "0" * 64 + "' WHERE path='pkg/core.py'")
    connection.commit()
    connection.close()
    with pytest.raises(CodeIndexError) as exc:
        index.lookup("helper")
    assert exc.value.code == "corrupt"


def test_status_and_lookup_never_parse(index: CodeIndex, monkeypatch):
    index.build()

    def fail(_snapshot):
        raise AssertionError("lookup/status must not parse")

    monkeypatch.setattr(index_module, "extract_python_facts", fail)
    assert index.status().freshness.value == "current"
    assert index.lookup("helper").hits


def test_clear_is_per_checkout(checkout: Path, cache: Path, tmp_path: Path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "a.py").write_text("def a():\n    return 1\n")
    one = CodeIndex(checkout, cache_base=cache)
    two = CodeIndex(other, cache_base=cache)
    one.build()
    two.build()
    assert one.clear() is True
    assert one.status().health.value == "missing"
    assert two.status().freshness.value == "current"
    assert one.clear() is False
