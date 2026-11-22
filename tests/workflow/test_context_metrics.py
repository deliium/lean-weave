"""Safe, non-persistent workflow context telemetry."""

from __future__ import annotations

import json
from pathlib import Path

from lean_weave.config.models import ContextCacheConfig, ContextConfig
from lean_weave.context_cache.models import ArtifactKind, CacheKey, CacheProof
from lean_weave.context_cache.service import SqliteContextCache
from lean_weave.workflow.context_metrics import (
    MAX_ORDINALS,
    CacheOperationCounts,
    ContextStageEvent,
    RecordingContextObserver,
    _AttemptTelemetry,
    emit,
    safe_section_estimates,
)


def test_fragment_ordinals_are_stable_and_bounded():
    telemetry = _AttemptTelemetry(stage="implement", mode="v2")
    first = telemetry.record_fragment("internal:path:digest", "definition")
    again = telemetry.record_fragment("internal:path:digest", "definition")
    assert first == again
    for index in range(MAX_ORDINALS + 10):
        telemetry.record_fragment(f"internal:{index}", "range")
    event = telemetry.freeze()
    assert len(event.fragments) == MAX_ORDINALS
    assert {item.ordinal for item in event.fragments} == set(range(MAX_ORDINALS))


def test_section_estimates_reject_unknown_and_negative_names():
    estimates = safe_section_estimates(
        [("system", 3), ("wire", 9), ("unknown", 2), ("task", -1)]
    )
    assert estimates == {"system": 3, "wire": 9, "task": 0}


def test_cache_observations_separate_unknown_from_zero():
    counts = CacheOperationCounts(available=False)
    counts.observe("facts", "hit")
    event = _AttemptTelemetry(cache=counts).freeze()
    assert event.cache_observations_available is False
    assert event.cache_hits == 1
    assert event.cache_misses == 0


def test_observer_failure_does_not_raise_or_mutate_event():
    class Broken:
        def observe(self, event):
            raise RuntimeError("boom")

    telemetry = _AttemptTelemetry(stage="plan", mode="v2", wire_input_estimate=7)
    emit(Broken(), telemetry.freeze())


def test_no_source_or_credential_canary_in_debug(caplog):
    observer = RecordingContextObserver()
    telemetry = _AttemptTelemetry(
        stage="implement",
        mode="v2",
        wire_input_estimate=11,
        retrieval_ms=2,
    )
    telemetry.record_fragment("secret-looking-internal-value", "definition")
    with caplog.at_level("DEBUG"):
        emit(observer, telemetry.freeze())
    text = caplog.text
    assert "secret-looking-internal-value" not in text
    assert "BEGIN RSA PRIVATE KEY" not in text
    assert "src/" not in text
    assert observer.events[0].wire_input_estimate == 11


def test_sqlite_cache_observer_receives_bypass_without_creating_storage(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "repo"
    root.mkdir()
    base = tmp_path / "cache"
    base.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CACHE_HOME", str(base))
    cache = SqliteContextCache(
        root,
        config=ContextCacheConfig(enabled=True),
        context_config=ContextConfig(),
        cache_base=base,
    )
    seen: list[tuple[str, str, str | None]] = []
    cache.observer = lambda kind, result, reason: seen.append((kind, result, reason))
    key = CacheKey.create(ArtifactKind.FACTS, inputs={"path": "src/a.py"})
    proof = CacheProof.create(
        ArtifactKind.FACTS, source_revision="sha256:" + "a" * 64
    )

    class Codec:
        def encode(self, value):
            return json.dumps(value).encode()

        def decode(self, payload):
            return json.loads(payload)

        def validate(self, value, proof):
            return True

    outcome = cache.lookup(key, proof, codec=Codec())
    assert outcome.result.value == "miss"
    assert seen == [("facts", "miss", "absent")]
    assert not (base / "lean-weave").exists()
