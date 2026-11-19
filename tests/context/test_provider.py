from pathlib import Path

import pytest

from lean_weave.app import AppContext, build_app_context, create_context_provider
from lean_weave.config import AppConfig
from lean_weave.context.provider import DeterministicContextProvider
from lean_weave.context.repository import Repository
from lean_weave.errors import ForbiddenContext, InvalidContextRequest
from lean_weave.schemas import (
    FeaturePlan,
    FeatureState,
    ImplementationTask,
    Stage,
    VerificationResult,
    VerificationStatus,
)

from .conftest import write_files


def _plan(testing: bool = True) -> FeaturePlan:
    return FeaturePlan(
        goal="Implement the helper",
        settings={"testing": testing},
        tasks=[
            ImplementationTask(
                id="t1",
                title="Wire helper",
                description="Implement helper in pkg/a.py",
                files=["pkg/a.py"],
            )
        ],
    )


def _state(stage: Stage, *, testing: bool = True, verification=None) -> FeatureState:
    return FeatureState(
        feature_id="feature",
        stage=stage,
        request="Implement the helper",
        plan=_plan(testing),
        active_task_id="t1",
        verification=verification,
    )


@pytest.fixture
def provider_project(project: Path):
    write_files(
        project,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "from .b import b\n\ndef a():\n    return b()\n",
            "pkg/b.py": "def b():\n    return 1\n",
            "tests/test_a.py": "from pkg.a import a\n\ndef test_a():\n    assert a() == 1\n",
            "noise.py": "def a():\n    return 999\n",
        },
    )
    return project


def test_plan_never_scans_or_runs_subprocesses(monkeypatch, project: Path):
    provider = DeterministicContextProvider(project)
    monkeypatch.setattr(provider, "_repository", lambda: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(
        "lean_weave.context.provider.build_python_index",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()),
    )
    state = FeatureState(feature_id="f", stage=Stage.PLAN, request="Plan the helper")
    bundle = provider.build("plan", state)
    assert bundle["stage"] == "plan"
    assert bundle["file_excerpts"] == {}
    assert bundle["messages"][1]["role"] == "user"
    assert "Plan the helper" in bundle["user_prompt"]


def test_verify_never_opens_source(monkeypatch, project: Path):
    provider = DeterministicContextProvider(project)
    monkeypatch.setattr(provider, "_repository", lambda: (_ for _ in ()).throw(AssertionError()))
    state = _state(Stage.VERIFY)
    bundle = provider.build("verify", state)
    assert bundle["file_excerpts"] == {}
    assert "Implement the helper" in bundle["user_prompt"]


def test_stage_must_agree_with_state(provider_project: Path):
    provider = DeterministicContextProvider(provider_project)
    state = _state(Stage.IMPLEMENT)
    with pytest.raises(ForbiddenContext):
        provider.build("verify", state)
    with pytest.raises(ForbiddenContext):
        provider.build("nonsense", state)


def test_implement_reads_only_scope_and_respects_testing_flag(
    provider_project: Path, monkeypatch
):
    reads: list[str] = []
    original = Repository._admit

    def recording(self, path):
        reads.append(path)
        return original(self, path)

    monkeypatch.setattr(Repository, "_admit", recording)
    provider = DeterministicContextProvider(provider_project)
    bundle = provider.build("implement", _state(Stage.IMPLEMENT, testing=True))
    read_paths = set(reads)
    assert "pkg/a.py" in read_paths
    assert "pkg/b.py" in read_paths
    assert "tests/test_a.py" in read_paths
    assert "noise.py" not in read_paths
    assert bundle["file_excerpts"]
    assert all(path != "noise.py" for path in bundle["file_excerpts"])

    reads.clear()
    provider = DeterministicContextProvider(provider_project)
    provider.build("implement", _state(Stage.IMPLEMENT, testing=False))
    assert "tests/test_a.py" not in set(reads)


def test_implement_requires_unique_active_task(provider_project: Path):
    provider = DeterministicContextProvider(provider_project)
    state = _state(Stage.IMPLEMENT)
    state.active_task_id = None
    with pytest.raises(InvalidContextRequest):
        provider.build("implement", state)

    state = _state(Stage.IMPLEMENT)
    state.plan.tasks.append(state.plan.tasks[0])
    with pytest.raises(InvalidContextRequest):
        provider.build("implement", state)


def test_implement_without_eligible_task_paths_uses_metadata(provider_project: Path):
    provider = DeterministicContextProvider(provider_project)
    state = _state(Stage.IMPLEMENT)
    state.plan.tasks[0].files = ["missing.py", ".env"]
    bundle = provider.build("implement", state)
    assert bundle["file_excerpts"] == {}
    assert "plan.goal" in bundle["user_prompt"]


def test_retry_requires_failed_verification(provider_project: Path):
    provider = DeterministicContextProvider(provider_project)
    state = _state(Stage.RETRY, verification=None)
    with pytest.raises(InvalidContextRequest):
        provider.build("retry", state)

    passed = VerificationResult(status=VerificationStatus.PASS, affected_files=["pkg/a.py"])
    state = _state(Stage.RETRY, verification=passed)
    with pytest.raises(InvalidContextRequest):
        provider.build("retry", state)


def test_retry_ignores_arbitrary_diagnostics_and_forbidden_paths(provider_project: Path):
    verification = VerificationResult(
        status=VerificationStatus.FAIL,
        blocking=True,
        affected_files=["pkg/a.py", ".env", "../outside.py"],
        blockers=[{"raw_log": "RAW_LOG_CANARY"}],
        gate_summaries=["GATE_CANARY"],
        suggested_next="SUGGESTION_CANARY",
    )
    provider = DeterministicContextProvider(provider_project)
    state = _state(Stage.RETRY, verification=verification)
    bundle = provider.build("retry", state)
    rendered = "\n".join(message["content"] or "" for message in bundle["messages"])
    assert "RAW_LOG_CANARY" not in rendered
    assert "GATE_CANARY" not in rendered
    assert "SUGGESTION_CANARY" not in rendered
    assert set(bundle["file_excerpts"]) == {"pkg/a.py"}
    assert any(
        omission.reason.value == "stage_scope" for omission in bundle["pack"].omissions
    )


def test_provider_does_not_mutate_state(provider_project: Path):
    provider = DeterministicContextProvider(provider_project)
    state = _state(Stage.IMPLEMENT)
    before = state.model_dump(mode="json")
    provider.build("implement", state)
    assert state.model_dump(mode="json") == before


def test_create_context_provider_honors_injection_and_is_lazy(monkeypatch):
    sentinel = object()
    config = AppConfig()
    context = AppContext(config, None, context_provider=sentinel)
    assert create_context_provider(context) is sentinel

    lazy = AppContext(config, None)
    monkeypatch.setattr(
        Repository, "__init__", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError())
    )
    provider = create_context_provider(lazy)
    assert isinstance(provider, DeterministicContextProvider)


def test_build_app_context_does_not_scan(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        Repository, "__init__", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError())
    )
    context = build_app_context()
    assert context.config.context.stage_prompt_tokens_max == 6000
