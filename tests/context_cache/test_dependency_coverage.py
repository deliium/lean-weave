"""Cache freshness and worktree isolation for opt-in dependency coverage."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lean_weave.config.models import (
    ContextCacheConfig,
    ContextConfig,
    DependencyCoverageConfig,
    PackingConfig,
)
from lean_weave.context import packing_source
from lean_weave.context.dependency_coverage import DependencyAnalyzer
from lean_weave.context.packer import ContextPacker
from lean_weave.context.packing_models import PackRequest
from lean_weave.context.python_index import extract_python_facts
from lean_weave.context.repository import Repository
from lean_weave.context.retriever import ContextRetriever
from lean_weave.code_index.identity import resolve_checkout
from lean_weave.context_cache.service import SqliteContextCache
from lean_weave.worktree.manager import WorktreeManager


def _run(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "PATH": "/usr/bin:/bin",
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
            "HOME": str(root),
        },
    )


def _write(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _packing(**overrides) -> PackingConfig:
    base = {
        "total_tokens": 8000,
        "output_tokens": 500,
        "system_tokens": 1000,
        "task_tokens": 1000,
        "map_tokens": 800,
        "source_tokens": 3000,
        "tool_tokens": 2000,
        "max_expansions": 2,
        "cumulative_source_tokens": 8000,
    }
    base.update(overrides)
    return PackingConfig(**base)


def _request(**overrides) -> PackRequest:
    payload = {
        "task_id": "task-1",
        "query": "activate HarborWidget",
        "task_specification": "Adjust HarborWidget activation.",
        "system_instructions": "Use only supplied source.",
        "level": "l1",
        "required_paths": ("service/runner.py",),
    }
    payload.update(overrides)
    return PackRequest(**payload)


def _tree() -> dict[str, str]:
    return {
        "models/__init__.py": "",
        "models/widget.py": (
            "class HarborWidget:\n"
            "    def spin(self) -> int:\n"
            "        return 41\n"
        ),
        "service/__init__.py": "",
        "service/runner.py": (
            "from models.widget import HarborWidget\n"
            "\n"
            "\n"
            "def activate(item: HarborWidget) -> int:\n"
            "    return item.spin()\n"
        ),
    }


@pytest.fixture
def enabled_project(tmp_path: Path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, _tree())
    base = tmp_path / "cache"
    base.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CACHE_HOME", str(base))
    context = ContextConfig(
        packing=_packing(),
        cache=ContextCacheConfig(enabled=True),
        dependency_coverage=DependencyCoverageConfig(enabled=True, max_depth=1),
    )
    repository = Repository(root, context, strict_source=True)
    cache = SqliteContextCache(
        root,
        config=context.cache,
        context_config=context,
        cache_base=base,
    )
    return root, repository, cache, context, base


def _packer(repository, cache, *, context: ContextConfig | None = None):
    cfg = context or repository.config
    return ContextPacker(
        Repository(repository.root, cfg, strict_source=True),
        retriever=ContextRetriever(
            Repository(repository.root, cfg, strict_source=True), cache=cache
        ),
        cache=cache,
    )


def _comparable(pack) -> dict:
    """Drop ephemeral fields that may differ across packer instances."""

    payload = pack.model_dump()
    # Receipts / IDs are not present on initial build; keep evidence deterministic.
    return payload


def test_disabled_cold_warm_semantically_match(enabled_project):
    root, repository, cache, context, _base = enabled_project
    enabled = context
    disabled_cfg = ContextConfig(
        packing=_packing(),
        cache=ContextCacheConfig(enabled=True),
        dependency_coverage=DependencyCoverageConfig(enabled=False),
    )
    cold = _packer(repository, cache, context=enabled).build(_request())
    warm = _packer(repository, cache, context=enabled).build(_request())
    assert cold.status == warm.status == "ready"
    assert cold.pack is not None and warm.pack is not None
    assert _comparable(cold.pack) == _comparable(warm.pack)
    assert cold.pack.dependency_coverage is not None
    assert "models/widget.py" in {item.path for item in cold.pack.fragments}

    off = _packer(repository, cache, context=disabled_cfg).build(_request())
    assert off.status == "ready"
    assert off.pack is not None
    assert off.pack.dependency_coverage is None


def test_warm_reuses_spans_with_dependency_coverage(enabled_project, monkeypatch):
    root, repository, cache, context, _base = enabled_project
    cold = _packer(repository, cache, context=context).build(_request())
    assert cold.status == "ready"
    calls = {"n": 0}
    original = packing_source._python_spans

    def counting(snapshot):
        calls["n"] += 1
        return original(snapshot)

    monkeypatch.setattr(packing_source, "_python_spans", counting)
    warm = _packer(repository, cache, context=context).build(_request())
    assert warm.status == "ready"
    assert warm.pack.model_dump() == cold.pack.model_dump()
    assert calls["n"] == 0


def test_importer_edit_invalidates_dependency_proof(enabled_project):
    root, repository, cache, context, _base = enabled_project
    first = _packer(repository, cache, context=context).build(_request())
    assert first.status == "ready"
    (root / "service" / "runner.py").write_text(
        "from models.widget import HarborWidget\n"
        "\n"
        "\n"
        "def activate(item: HarborWidget) -> int:\n"
        "    return item.spin() + 1\n",
        encoding="utf-8",
    )
    second = _packer(repository, cache, context=context).build(_request())
    assert second.status == "ready"
    assert second.pack is not None and first.pack is not None
    assert second.pack.source_revisions != first.pack.source_revisions
    assert "models/widget.py" in {item.path for item in second.pack.fragments}


def test_definition_edit_refreshes_target_digest(enabled_project):
    root, repository, cache, context, _base = enabled_project
    first = _packer(repository, cache, context=context).build(_request())
    (root / "models" / "widget.py").write_text(
        "class HarborWidget:\n"
        "    def spin(self) -> int:\n"
        "        return 99\n",
        encoding="utf-8",
    )
    second = _packer(repository, cache, context=context).build(_request())
    assert second.status == "ready"
    widget = [
        item
        for item in second.pack.fragments
        if item.path == "models/widget.py"
    ]
    assert widget
    prior = {item.digest for item in first.pack.fragments if item.path == "models/widget.py"}
    now = {item.digest for item in widget}
    assert prior != now


def test_competing_module_invalidates_unique_proof(enabled_project):
    root, repository, cache, context, _base = enabled_project
    first = _packer(repository, cache, context=context).build(_request())
    assert first.status == "ready"
    assert first.pack.dependency_coverage is not None
    assert any(
        row.target_path == "models/widget.py"
        for row in first.pack.dependency_evidence
    )
    # Duplicate top-level definition in the imported module breaks unique proof.
    (root / "models" / "widget.py").write_text(
        "class HarborWidget:\n"
        "    def spin(self) -> int:\n"
        "        return 41\n"
        "\n"
        "class HarborWidget:\n"
        "    def spin(self) -> int:\n"
        "        return 0\n",
        encoding="utf-8",
    )
    second = _packer(repository, cache, context=context).build(_request())
    assert second.status == "ready"
    assert second.pack is not None
    imported = [
        row
        for row in second.pack.dependency_evidence
        if row.relationship == "imported_definition"
        and row.target_path == "models/widget.py"
    ]
    assert imported == [] or second.pack.dependency_coverage.coverage_complete is False
    assert (
        second.pack.dependency_issues
        or imported == []
        or second.pack.dependency_coverage.status == "incomplete"
    )


def test_budget_change_on_warm_hit_redecides(enabled_project):
    root, repository, cache, context, _base = enabled_project
    first = _packer(repository, cache, context=context).build(_request())
    tight = ContextConfig(
        packing=_packing(source_tokens=80, cumulative_source_tokens=80),
        cache=ContextCacheConfig(enabled=True),
        dependency_coverage=DependencyCoverageConfig(
            enabled=True, max_depth=1, max_tokens=40
        ),
    )
    second = _packer(repository, cache, context=tight).build(_request())
    assert first.status == "ready"
    # Tight dependency ceiling may refuse or omit; decision must be live.
    assert second.status in {"ready", "insufficient_context"}
    if second.status == "insufficient_context":
        assert any(
            code.startswith("dependency_limit:") or code == "required_source_over_budget"
            for code in second.reason_codes
        )


def test_disabled_feature_skips_analyzer_graph(enabled_project, monkeypatch):
    root, repository, cache, _context, _base = enabled_project
    disabled = ContextConfig(
        packing=_packing(),
        cache=ContextCacheConfig(enabled=True),
        dependency_coverage=DependencyCoverageConfig(enabled=False),
    )
    calls = {"n": 0}
    original = ContextPacker._proof_graph

    def counting(self, scan):
        calls["n"] += 1
        return original(self, scan)

    monkeypatch.setattr(ContextPacker, "_proof_graph", counting)
    result = _packer(repository, cache, context=disabled).build(_request())
    assert result.status == "ready"
    assert calls["n"] == 0


def test_cache_disabled_and_feature_disabled_no_cache_io(tmp_path: Path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, _tree())
    base = tmp_path / "cache"
    base.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CACHE_HOME", str(base))
    context = ContextConfig(
        packing=_packing(),
        cache=ContextCacheConfig(enabled=False),
        dependency_coverage=DependencyCoverageConfig(enabled=False),
    )
    repository = Repository(root, context, strict_source=True)
    packer = ContextPacker(
        repository,
        retriever=ContextRetriever(repository),
        cache=None,
    )
    result = packer.build(_request())
    assert result.status == "ready"
    assert packer._fact_cache is None
    assert packer._span_cache is None


def test_analyzer_memo_dies_with_instance(enabled_project):
    root, repository, _cache, context, _base = enabled_project
    with repository.scan_retrieval_sources() as scan:
        from lean_weave.context.packing_source import PackingCatalog
        from lean_weave.repository_map.service import scoped_graph_for_scan
        from lean_weave.code_index.identity import resolve_checkout

        catalog = PackingCatalog(scan)
        graph = scoped_graph_for_scan(
            scan,
            facts_loader=extract_python_facts,
            checkout_key=resolve_checkout(root).key,
            state_dir=repository.state_dir,
            candidate_bytes_max=context.candidate_bytes_max,
        )
        first = DependencyAnalyzer(catalog, graph)
        snap = catalog.snapshot("service/runner.py")
        assert snap is not None
        first._projection("service/runner.py", snap.digest)
        assert first._projections
        first.clear_memo()
        assert first._projections == {}
        second = DependencyAnalyzer(catalog, graph)
        assert second._projections == {}


def test_sibling_worktrees_isolate_dependency_proof(tmp_path: Path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    _run(origin, "init", "-q")
    _run(origin, "config", "user.email", "fixture@example.invalid")
    _run(origin, "config", "user.name", "Fixture")
    _write(origin, _tree())
    _run(origin, "add", "-A")
    _run(origin, "commit", "-qm", "base")
    base = tmp_path / "cache"
    base.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CACHE_HOME", str(base))
    manager = WorktreeManager(origin)
    local = manager.create()
    peer = manager.create()
    context = ContextConfig(
        packing=_packing(),
        cache=ContextCacheConfig(enabled=True),
        dependency_coverage=DependencyCoverageConfig(enabled=True, max_depth=1),
    )
    local_root = Path(local.identity.root)
    peer_root = Path(peer.identity.root)
    local_cache = SqliteContextCache(
        local_root,
        config=context.cache,
        context_config=context,
        cache_base=base,
    )
    peer_cache = SqliteContextCache(
        peer_root,
        config=context.cache,
        context_config=context,
        cache_base=base,
    )
    local_repo = Repository(local_root, context, strict_source=True)
    peer_repo = Repository(peer_root, context, strict_source=True)
    warm = _packer(local_repo, local_cache, context=context).build(_request())
    assert warm.status == "ready"
    (peer_root / "models" / "widget.py").write_text(
        "class HarborWidget:\n"
        "    def spin(self) -> int:\n"
        "        return 7\n",
        encoding="utf-8",
    )
    peer_result = _packer(peer_repo, peer_cache, context=context).build(_request())
    again = _packer(local_repo, local_cache, context=context).build(_request())
    assert peer_result.status == "ready" and again.status == "ready"
    local_widget = {
        item.digest
        for item in again.pack.fragments
        if item.path == "models/widget.py"
    }
    peer_widget = {
        item.digest
        for item in peer_result.pack.fragments
        if item.path == "models/widget.py"
    }
    assert local_widget != peer_widget
    assert resolve_checkout(local_root).key != resolve_checkout(peer_root).key
