from __future__ import annotations

from pathlib import Path

import pytest

from lean_weave.config.models import ContextCacheConfig, ContextConfig
from lean_weave.context.repository import Repository
from lean_weave.context_cache.facts import CachedFactLoader
from lean_weave.context_cache.service import CachePolicy, SqliteContextCache


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("def one():\n    return 1\n", encoding="utf-8")
    return Repository(root, ContextConfig(), strict_source=True)


@pytest.fixture
def cache(repo: Repository, tmp_path: Path, monkeypatch) -> SqliteContextCache:
    base = tmp_path / "cache"
    base.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CACHE_HOME", str(base))
    return SqliteContextCache(
        repo.root,
        config=ContextCacheConfig(enabled=True),
        context_config=repo.config,
        cache_base=base,
    )


def _policy(cache: SqliteContextCache) -> CachePolicy:
    return cache.policy


def test_warm_loader_skips_extraction(repo: Repository, cache: SqliteContextCache, monkeypatch):
    snapshot = repo.read("src/a.py")
    calls = {"n": 0}
    import lean_weave.context_cache.facts as facts_module
    original = facts_module.extract_python_facts

    def counting(value):
        calls["n"] += 1
        return original(value)

    monkeypatch.setattr(facts_module, "extract_python_facts", counting)
    policy = _policy(cache)
    with CachedFactLoader(cache, repo, policy=policy) as load:
        first = load(snapshot)
    assert calls["n"] == 1

    with CachedFactLoader(cache, repo, policy=policy) as load:
        second = load(snapshot)
    assert calls["n"] == 1
    assert first == second


def test_source_change_invalidates_one_path(repo: Repository, cache: SqliteContextCache):
    policy = _policy(cache)
    first = repo.read("src/a.py")
    with CachedFactLoader(cache, repo, policy=policy) as load:
        load(first)
    (repo.root / "src" / "a.py").write_text(
        "def two():\n    return 2\n", encoding="utf-8"
    )
    repo.refresh()
    second = repo.read("src/a.py")
    with CachedFactLoader(cache, repo, policy=policy) as load:
        facts = load(second)
    assert [item.simple_name for item in facts.definitions] == ["two"]


def test_parse_limit_is_not_published(repo: Repository, cache: SqliteContextCache, monkeypatch):
    import lean_weave.context_cache.facts as facts_module
    from lean_weave.context.python_index import PythonFacts

    monkeypatch.setattr(
        facts_module,
        "extract_python_facts",
        lambda snapshot: PythonFacts(status="parse_limit", line_count=snapshot.lines),
    )
    snapshot = repo.read("src/a.py")
    with CachedFactLoader(cache, repo, policy=_policy(cache)) as load:
        facts = load(snapshot)
    assert facts.status == "parse_limit"
    assert cache.status().entries in {None, 0}


def test_disabled_mode_does_not_create_storage(tmp_path: Path, monkeypatch):
    from lean_weave.context_cache import DisabledContextCache

    base = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(base))
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    repository = Repository(root, ContextConfig(), strict_source=True)
    cache = DisabledContextCache()
    with CachedFactLoader(
        cache,
        repository,
        policy=CachePolicy.from_context(ContextConfig(), state_dir=".lean-weave"),
    ) as load:
        load(repository.read("a.py"))
    assert not base.exists()
