from __future__ import annotations

from pathlib import Path

import pytest

from lean_weave.app import (
    AppContext,
    create_context_packer,
    create_retrieval_provider,
)
from lean_weave.config.models import AppConfig
from lean_weave.context.packing_models import PackRequest
from lean_weave.context.retrieval_models import RetrievalRequest


@pytest.fixture
def project(tmp_path: Path, monkeypatch):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "config.py").write_text(
        "VERSION = '1.0'\n\n\ndef banner():\n    return VERSION\n", encoding="utf-8"
    )
    (root / "src" / "app.py").write_text(
        "from src.config import banner\n\ndef run():\n    return banner()\n",
        encoding="utf-8",
    )
    (root / "src" / "util.py").write_text(
        "def helper(value):\n    return value\n", encoding="utf-8"
    )
    base = tmp_path / "cache"
    base.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CACHE_HOME", str(base))
    config = AppConfig.model_validate({"context": {"cache": {"enabled": True}}})
    return root, base, AppContext(config=config, config_source=None)


def _pack_request() -> PackRequest:
    return PackRequest(
        task_id="task-1",
        query="banner VERSION config",
        task_specification="Change the banner version.",
        system_instructions="Be careful.",
        level="l1",
    )


def test_cold_warm_and_disabled_agree(project, monkeypatch):
    root, base, context = project

    def fail(*_args, **_kwargs):
        raise AssertionError("cache operations must not construct an LLM provider")

    monkeypatch.setattr("lean_weave.app.create_llm_provider", fail)

    cold_provider = create_retrieval_provider(context, workspace_root=root, cache_base=base)
    cold = cold_provider.retrieve(RetrievalRequest(query="banner VERSION"))
    warm_provider = create_retrieval_provider(context, workspace_root=root, cache_base=base)
    warm = warm_provider.retrieve(RetrievalRequest(query="banner VERSION"))
    assert warm == cold

    cold_pack = create_context_packer(context, workspace_root=root, cache_base=base).build(
        _pack_request()
    )
    warm_pack = create_context_packer(context, workspace_root=root, cache_base=base).build(
        _pack_request()
    )
    assert warm_pack.status == cold_pack.status == "ready"
    assert warm_pack.pack.model_dump() == cold_pack.pack.model_dump()

    disabled = AppContext(config=AppConfig(), config_source=None)
    disabled_result = create_retrieval_provider(
        disabled, workspace_root=root, cache_base=base
    ).retrieve(RetrievalRequest(query="banner VERSION"))
    assert disabled_result == cold


def test_source_edit_matches_uncached(project):
    root, base, context = project
    request = RetrievalRequest(query="banner VERSION")
    first = create_retrieval_provider(context, workspace_root=root, cache_base=base).retrieve(
        request
    )
    (root / "src" / "config.py").write_text(
        "VERSION = '2.0'\n\n\ndef banner():\n    return VERSION * 2\n", encoding="utf-8"
    )
    second = create_retrieval_provider(context, workspace_root=root, cache_base=base).retrieve(
        request
    )
    disabled = AppContext(config=AppConfig(), config_source=None)
    uncached = create_retrieval_provider(
        disabled, workspace_root=root, cache_base=base
    ).retrieve(request)
    assert second == uncached
    assert second != first


def test_other_checkout_starts_cold(project, tmp_path):
    root, base, context = project
    request = RetrievalRequest(query="banner VERSION")
    first = create_retrieval_provider(context, workspace_root=root, cache_base=base).retrieve(
        request
    )
    other = tmp_path / "other"
    (other / "src").mkdir(parents=True)
    (other / "src" / "config.py").write_text(
        (root / "src" / "config.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (other / "src" / "app.py").write_text(
        (root / "src" / "app.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (other / "src" / "util.py").write_text(
        (root / "src" / "util.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    from lean_weave.app import create_context_cache

    before = create_context_cache(
        context, workspace_root=other, cache_base=base
    ).status()
    assert before.entries in {None, 0}
    second = create_retrieval_provider(
        context, workspace_root=other, cache_base=base
    ).retrieve(request)
    assert second == first
