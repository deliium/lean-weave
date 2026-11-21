"""Checked generation reuse against an already admitted retrieval scan."""

from __future__ import annotations

import contextlib

import pytest

import lean_weave.code_index.index as index_module
from lean_weave.code_index import CodeIndex
from lean_weave.config.models import ContextConfig
from lean_weave.context.repository import Repository
from lean_weave.errors import CodeIndexError


def _strict_repo(root, **overrides) -> Repository:
    return Repository(
        root,
        ContextConfig(**overrides),
        strict_source=True,
    )


def test_retrieval_scan_reads_generation_without_second_python_walk(
    index: CodeIndex, checkout, monkeypatch
):
    index.build()
    repository = _strict_repo(checkout)
    scanned = {"python": 0}

    original = Repository.scan_python_sources

    @contextlib.contextmanager
    def wrapped(self):
        scanned["python"] += 1
        with original(self) as scan:
            yield scan

    monkeypatch.setattr(index_module.Repository, "scan_python_sources", wrapped)
    with repository.scan_retrieval_sources() as scan:
        generation = index.read_generation_for_retrieval_scan(scan)
        assert generation.header.files >= 1
        assert scanned["python"] == 0


def test_incomplete_retrieval_scan_refuses_before_lease(index: CodeIndex, checkout, monkeypatch):
    index.build()
    entered = {"lease": 0}
    original = index_module._read_lease

    @contextlib.contextmanager
    def wrapped(location):
        entered["lease"] += 1
        with original(location):
            yield

    monkeypatch.setattr(index_module, "_read_lease", wrapped)
    with _strict_repo(checkout, visited_entries_max=1).scan_retrieval_sources() as scan:
        assert scan.complete is False
        with pytest.raises(CodeIndexError) as exc:
            index.read_generation_for_retrieval_scan(scan)
        assert exc.value.code == "incomplete"
    assert entered["lease"] == 0


def test_detached_scan_is_invalid_request(index: CodeIndex, checkout):
    index.build()
    with _strict_repo(checkout).scan_retrieval_sources() as scan:
        generation = index.read_generation_for_retrieval_scan(scan)
        assert generation.records
    with pytest.raises(CodeIndexError) as exc:
        index.read_generation_for_retrieval_scan(scan)
    assert exc.value.code == "invalid_request"


def test_policy_mismatch_is_invalid_request(index: CodeIndex, checkout):
    index.build()
    drifted = Repository(
        checkout,
        ContextConfig(exclusions=["*.bak"]),
        strict_source=True,
    )
    with drifted.scan_retrieval_sources() as scan:
        with pytest.raises(CodeIndexError) as exc:
            index.read_generation_for_retrieval_scan(scan)
        assert exc.value.code == "invalid_request"


def test_stale_digest_refuses_retrieval_generation(index: CodeIndex, checkout):
    index.build()
    (checkout / "pkg" / "core.py").write_text("def changed():\n    return 1\n")
    with _strict_repo(checkout).scan_retrieval_sources() as scan:
        with pytest.raises(CodeIndexError) as exc:
            index.read_generation_for_retrieval_scan(scan)
        assert exc.value.code == "stale"


def test_non_python_edit_does_not_invalidate_python_graph(index: CodeIndex, checkout):
    index.build()
    (checkout / "README.md").write_text("# notes\n", encoding="utf-8")
    with _strict_repo(checkout).scan_retrieval_sources() as scan:
        generation = index.read_generation_for_retrieval_scan(scan)
        assert any(item.path.endswith(".py") for item in generation.records)


def test_changed_witness_refuses_retrieval_generation(index: CodeIndex, checkout):
    index.build()
    with _strict_repo(checkout).scan_retrieval_sources() as scan:
        (checkout / "pkg" / "core.py").write_text("def changed():\n    return 2\n")
        with pytest.raises(CodeIndexError) as exc:
            index.read_generation_for_retrieval_scan(scan)
        assert exc.value.code in {"storage_unavailable", "stale"}
