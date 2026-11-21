"""Provider retrieve wiring stays opt-in and lazy."""

from __future__ import annotations

from pathlib import Path

from lean_weave.app import AppContext, create_context_provider, create_retrieval_provider
from lean_weave.config.models import AppConfig, ContextConfig
from lean_weave.context.provider import DeterministicContextProvider
from lean_weave.context.retrieval_models import RetrievalRequest, RetrievalResult

from .conftest import write_files


class _LegacyOnlyBuild:
    def build(self, stage, feature_state):
        return {"system": "", "user": "", "estimated_tokens": 0}


def test_retriever_factory_called_only_by_retrieve(project: Path):
    write_files(project, {"a.py": "def alpha():\n    return 1\n"})
    calls = {"factory": 0, "retrieve": 0}

    class FakeRetriever:
        def retrieve(self, request):
            calls["retrieve"] += 1
            return RetrievalResult(
                hits=(),
                task_kind=request.task_kind,
                kind=request.kind,
                top_k=request.top_k or 10,
            )

    def factory():
        calls["factory"] += 1
        return FakeRetriever()

    provider = DeterministicContextProvider(
        project,
        context_config=ContextConfig(),
        retriever_factory=factory,
    )
    assert calls["factory"] == 0
    provider.retrieve(RetrievalRequest(query="alpha"))
    assert calls == {"factory": 1, "retrieve": 1}
    provider.retrieve(RetrievalRequest(query="alpha"))
    assert calls == {"factory": 1, "retrieve": 2}


def test_create_context_provider_accepts_legacy_build_only(project: Path):
    context = AppContext(config=AppConfig(), config_source=None)
    legacy = _LegacyOnlyBuild()
    provider = create_context_provider(context, provider=legacy)
    assert provider is legacy


def test_create_retrieval_provider_is_concrete(project: Path):
    write_files(project, {"a.py": "def alpha():\n    return 1\n"})
    context = AppContext(config=AppConfig(), config_source=None)
    provider = create_retrieval_provider(context, workspace_root=project)
    assert isinstance(provider, DeterministicContextProvider)
    result = provider.retrieve(RetrievalRequest(query="alpha"))
    assert isinstance(result, RetrievalResult)
