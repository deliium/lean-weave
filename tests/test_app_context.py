from pathlib import Path

import pytest

from lean_weave.app import build_app_context, create_llm_provider
from lean_weave.config import ConfigError, init_config
from lean_weave.errors import DependencyUnavailable
from lean_weave.metrics.jsonl import JsonlMetricsCollector
from lean_weave.providers.deepseek import DeepSeekProvider
from lean_weave.schemas import FeatureState


class FakeStore:
    def load(self, feature_id: str) -> FeatureState:
        return FeatureState(feature_id=feature_id)

    def save(self, feature_state: FeatureState) -> None:
        self.saved = feature_state


def test_builder_paths_and_injection(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake = FakeStore()
    context = build_app_context(state_store=fake)
    assert context.config_source is None
    assert context.state_store is fake
    assert context.state_store.load("f").feature_id == "f"
    assert context.llm_provider is None
    with pytest.raises(ConfigError):
        build_app_context(Path("missing.yaml"))
    source = init_config()
    assert build_app_context().config_source == source
    assert build_app_context(source).config_source == source
    source.write_text("llm: [")
    with pytest.raises(ConfigError):
        build_app_context()


def test_all_typed_overrides(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # A fake may implement multiple protocols; identity is preserved in every slot.
    class Components(FakeStore):
        def complete(self, request):
            return {}

        def build(self, stage, feature_state):
            return {}

        def run(self, request):
            return request

        def record_usage(self, feature_id, stage, usage):
            pass

        def totals(self, feature_id):
            return None

        def run_feature(self, feature_id, request):
            return self.load(feature_id)

        def resume(self, feature_id, decision=None):
            return self.load(feature_id)

    fake = Components()
    context = build_app_context(
        llm_provider=fake, context_provider=fake, code_executor=fake,
        verification_runner=fake, state_store=fake, metrics_collector=fake,
        orchestrator=fake,
    )
    for name in (
        "llm_provider", "context_provider", "code_executor", "verification_runner",
        "state_store", "metrics_collector", "orchestrator",
    ):
        assert getattr(context, name) is fake
    assert not hasattr(context, "require")


def test_provider_construction_is_lazy_and_offline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    init_config()
    context = build_app_context()
    assert not (Path(".lean-weave") / "metrics").exists()
    provider = create_llm_provider(context)
    assert isinstance(provider, DeepSeekProvider)
    assert not (Path(".lean-weave") / "metrics").exists()
    assert provider._journal.journal_path == tmp_path / ".lean-weave" / "metrics" / "llm-usage.jsonl"
    assert not provider._journal.journal_path.exists()
    provider.close()


def test_injected_provider_precedence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class Injected:
        def complete(self, request):
            return {"message": {"role": "assistant", "content": "fake"}}

    fake = Injected()
    context = build_app_context(llm_provider=fake, metrics_collector=object())
    assert create_llm_provider(context) is fake


def test_legacy_collector_fails_closed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class Legacy:
        def record_usage(self, feature_id, stage, usage):
            pass

        def totals(self, feature_id):
            return None

    context = build_app_context(metrics_collector=Legacy())
    with pytest.raises(DependencyUnavailable, match="atomically"):
        create_llm_provider(context)


def test_injected_atomic_journal_is_used(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    journal = JsonlMetricsCollector(tmp_path / "state")
    context = build_app_context(metrics_collector=journal)
    provider = create_llm_provider(context)
    assert provider._journal is journal
    provider.close()
