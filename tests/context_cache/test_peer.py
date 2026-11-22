from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lean_weave.app import AppContext, open_peer_fact_reuse
from lean_weave.config.models import AppConfig, ContextCacheConfig, ContextConfig
from lean_weave.context.repository import Repository
from lean_weave.context_cache.facts import FactCache
from lean_weave.context_cache.service import CachePolicy, SqliteContextCache
from lean_weave.errors import WorktreeError
from lean_weave.worktree.manager import WorktreeManager


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
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


def _init_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    _run(root, "init", "-q")
    _run(root, "config", "user.email", "fixture@example.invalid")
    _run(root, "config", "user.name", "Fixture")
    (root / "pkg").mkdir()
    (root / "pkg" / "a.py").write_text("def shared():\n    return 1\n", encoding="utf-8")
    _run(root, "add", "pkg/a.py")
    _run(root, "commit", "-qm", "base")
    return root


@pytest.fixture
def pair(tmp_path: Path, monkeypatch):
    origin = _init_repo(tmp_path / "origin")
    base = tmp_path / "cache"
    base.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CACHE_HOME", str(base))
    manager = WorktreeManager(origin)
    local = manager.create()
    peer = manager.create()
    config = AppConfig.model_validate({"context": {"cache": {"enabled": True}}})
    context = AppContext(config=config, config_source=None)
    return manager, local, peer, context, base


def _warm_peer_cache(peer, context, base):
    cache = SqliteContextCache(
        peer.identity.root,
        config=context.config.context.cache,
        context_config=context.config.context,
        state_dir=context.config.workflow.state_dir,
        cache_base=base,
    )
    repository = Repository(
        peer.identity.root, context.config.context, strict_source=True
    )
    snapshot = repository.read("pkg/a.py")
    policy = CachePolicy.from_context(
        context.config.context, state_dir=context.config.workflow.state_dir
    )
    facts = FactCache(cache, policy=policy)
    batch = cache.begin_batch()
    from lean_weave.context.python_index import extract_python_facts

    value = extract_python_facts(snapshot)
    assert facts.publish(snapshot, value, batch=batch)
    assert cache.flush(batch)
    return cache, value


def test_authorized_peer_serves_facts_without_peer_writes(pair):
    manager, local, peer, context, base = pair
    peer_cache, expected = _warm_peer_cache(peer, context, base)
    before = peer_cache.stats()

    with open_peer_fact_reuse(
        manager,
        local_feature_id=local.feature_id,
        peer_feature_id=peer.feature_id,
        local_context=context,
        peer_context=context,
        allowed_paths=["pkg/a.py"],
        cache_base=base,
    ) as capability:
        repository = Repository(
            local.identity.root, context.config.context, strict_source=True
        )
        snapshot = repository.read("pkg/a.py")
        assert capability.load(snapshot) == expected

    after = peer_cache.stats()
    assert before.events == after.events


def test_default_local_lookup_is_isolated(pair):
    manager, local, peer, context, base = pair
    _warm_peer_cache(peer, context, base)
    local_cache = SqliteContextCache(
        local.identity.root,
        config=context.config.context.cache,
        context_config=context.config.context,
        state_dir=context.config.workflow.state_dir,
        cache_base=base,
    )
    repository = Repository(
        local.identity.root, context.config.context, strict_source=True
    )
    snapshot = repository.read("pkg/a.py")
    policy = CachePolicy.from_context(
        context.config.context, state_dir=context.config.workflow.state_dir
    )
    outcome = FactCache(local_cache, policy=policy).lookup(snapshot)
    assert not outcome.hit


def test_peer_rejects_scope_and_config_mismatch(pair):
    manager, local, peer, context, base = pair
    with pytest.raises(WorktreeError, match="peer_reuse_requires_scope"):
        with open_peer_fact_reuse(
            manager,
            local_feature_id=local.feature_id,
            peer_feature_id=peer.feature_id,
            local_context=context,
            allowed_paths=[],
            cache_base=base,
        ):
            pass
    other = AppContext(
        config=AppConfig.model_validate(
            {
                "context": {
                    "cache": {"enabled": True},
                    "candidate_bytes_max": 4096,
                }
            }
        ),
        config_source=None,
    )
    with pytest.raises(WorktreeError, match="peer_reuse_configuration_mismatch"):
        with open_peer_fact_reuse(
            manager,
            local_feature_id=local.feature_id,
            peer_feature_id=peer.feature_id,
            local_context=context,
            peer_context=other,
            allowed_paths=["pkg/a.py"],
            cache_base=base,
        ):
            pass
