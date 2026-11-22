from __future__ import annotations

import os
from pathlib import Path

import pytest

from lean_weave.code_index.identity import context_cache_location, resolve_checkout
from lean_weave.context_cache.models import (
    ArtifactKind,
    CacheKey,
    CacheProof,
    DisabledContextCache,
    envelope_checksum,
)


def test_key_is_order_independent_for_mappings_but_not_sequences():
    first = CacheKey.create(
        ArtifactKind.FACTS,
        algorithm={"b": 2, "a": 1},
        config={"x": [1, 2]},
        inputs={"path": "a.py"},
    )
    second = CacheKey.create(
        ArtifactKind.FACTS,
        algorithm={"a": 1, "b": 2},
        config={"x": [1, 2]},
        inputs={"path": "a.py"},
    )
    assert first == second

    ordered = CacheKey.create(ArtifactKind.FACTS, inputs={"terms": ["a", "b"]})
    reversed_terms = CacheKey.create(ArtifactKind.FACTS, inputs={"terms": ["b", "a"]})
    assert ordered != reversed_terms


def test_versions_config_and_kind_change_the_key():
    base = CacheKey.create(ArtifactKind.FACTS, algorithm={"v": 1}, inputs={"p": "a.py"})
    version = CacheKey.create(ArtifactKind.FACTS, algorithm={"v": 2}, inputs={"p": "a.py"})
    config = CacheKey.create(
        ArtifactKind.FACTS, algorithm={"v": 1}, config={"cap": 2}, inputs={"p": "a.py"}
    )
    kind = CacheKey.create(ArtifactKind.IMPORTS, algorithm={"v": 1}, inputs={"p": "a.py"})
    assert len({base.digest, version.digest, config.digest, kind.digest}) == 4


def test_proof_changes_with_revision_and_inputs():
    first = CacheProof.create(
        ArtifactKind.FACTS, source_revision="sha256:" + "a" * 64, inputs={"d": 1}
    )
    second = CacheProof.create(
        ArtifactKind.FACTS, source_revision="sha256:" + "b" * 64, inputs={"d": 1}
    )
    third = CacheProof.create(
        ArtifactKind.FACTS, source_revision="sha256:" + "a" * 64, inputs={"d": 2}
    )
    assert len({first.digest, second.digest, third.digest}) == 3


def test_envelope_checksum_depends_on_every_field():
    args = dict(
        checkout_key="c" * 64,
        kind=ArtifactKind.FACTS,
        key_digest="sha256:" + "1" * 64,
        proof_digest="sha256:" + "2" * 64,
        source_revision="sha256:" + "3" * 64,
        payload=b"{}",
    )
    baseline = envelope_checksum(**args)
    assert envelope_checksum(**args) == baseline
    assert envelope_checksum(**{**args, "payload": b"[]"}) != baseline
    assert envelope_checksum(**{**args, "kind": ArtifactKind.IMPORTS}) != baseline


def test_disabled_cache_performs_no_io(tmp_path: Path, monkeypatch):
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_root))
    cache = DisabledContextCache()
    key = CacheKey.create(ArtifactKind.FACTS, inputs={"p": "a.py"})
    proof = CacheProof.create(ArtifactKind.FACTS)
    outcome = cache.lookup(key, proof, codec=_Codec())
    assert outcome.result.value == "bypass"
    assert cache.publish(key, proof, {"x": 1}, codec=_Codec()) is False
    assert cache.status().health.value == "disabled"
    assert cache.stats().health.value == "disabled"
    assert cache.clear() is False
    assert not cache_root.exists()


def test_distinct_roots_have_distinct_namespaces(tmp_path: Path, monkeypatch):
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_root))
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    location_a = context_cache_location(resolve_checkout(first))
    location_b = context_cache_location(resolve_checkout(second))
    assert location_a.checkout_key != location_b.checkout_key
    assert location_a.checkout_dir != location_b.checkout_dir
    assert not cache_root.exists()


class _Codec:
    kind = ArtifactKind.FACTS

    def encode(self, value):
        return b"{}"

    def decode(self, payload):
        return {}

    def validate(self, value, proof):
        return True
