from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from benchmarks.fixtures import load_manifest, materialize
from lean_weave.code_index import CodeIndex
from lean_weave.config.models import ContextConfig
from lean_weave.context.models import iter_lines
from lean_weave.context.provider import DeterministicContextProvider
from lean_weave.schemas import FeaturePlan, FeatureState, ImplementationTask, Stage


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    return materialize("bug_fix", tmp_path / "origin").root


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    path = tmp_path.parent / f"{tmp_path.name}-cache"
    path.mkdir(mode=0o700)
    return path


def test_real_fixture_build_lookup_and_range(fixture_repo: Path, cache: Path):
    manifest = load_manifest("bug_fix")
    index = CodeIndex(fixture_repo, cache_base=cache)
    update = index.build()
    assert update.count("syntax_failed") == 0
    assert index.status().freshness.value == "current"

    hits = index.lookup("add", path="src/calc.py").hits
    assert len(hits) == 1
    source = (fixture_repo / "src" / "calc.py").read_text(encoding="utf-8")
    lines = iter_lines(source)
    hit = hits[0]
    assert hit.revision == update.revision
    assert hit.digest == "sha256:" + hashlib.sha256(source.encode()).hexdigest()
    assert "def add" in "\n".join(lines[hit.start_line - 1 : hit.end_line])

    assert index.clear() is True
    assert index.status().health.value == "missing"


def test_linked_checkout_at_same_head_is_isolated(fixture_repo: Path, cache: Path, tmp_path: Path):
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(fixture_repo), "worktree", "add", "-q", "-b", "linked", str(linked), "HEAD"],
        check=True,
        capture_output=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(fixture_repo), "GIT_CONFIG_NOSYSTEM": "1"},
    )
    origin = CodeIndex(fixture_repo, cache_base=cache)
    checkout = CodeIndex(linked, cache_base=cache)
    assert origin._location().checkout_key != checkout._location().checkout_key
    assert origin.status().health.value == "missing"
    assert checkout.status().health.value == "missing"
    origin.build()
    assert checkout.status().health.value == "missing"
    checkout.build()
    (linked / "src" / "calc.py").write_text("def add(a, b):\n    return a - b + 1\n")
    assert checkout.status().freshness.value == "stale"
    checkout.update()
    assert checkout.status().freshness.value == "current"
    assert origin.status().freshness.value == "current"
    assert origin.clear() is True
    assert checkout.status().freshness.value == "current"


def _state(manifest) -> FeatureState:
    task = manifest.task_plan[0]
    return FeatureState(
        feature_id="fixture",
        stage=Stage.IMPLEMENT,
        request=manifest.request,
        plan=FeaturePlan(
            goal=manifest.request,
            settings={"testing": False},
            tasks=[
                ImplementationTask(
                    id=task["id"],
                    title=task["title"],
                    description=task["description"],
                    files=list(task["files"]),
                )
            ],
        ),
        active_task_id=task["id"],
    )


def test_warm_fixture_prompt_matches_cold(fixture_repo: Path, cache: Path):
    manifest = load_manifest("bug_fix")
    state = _state(manifest)
    cold = DeterministicContextProvider(fixture_repo, context_config=ContextConfig())
    cold_bundle = cold.build("implement", state)
    CodeIndex(fixture_repo, cache_base=cache).build()
    warm = DeterministicContextProvider(
        fixture_repo,
        context_config=ContextConfig(),
        fact_loader_factory=lambda: CodeIndex(
            fixture_repo, cache_base=cache
        ).fact_loader(),
    )
    warm_bundle = warm.build("implement", state)
    assert warm_bundle["messages"] == cold_bundle["messages"]
    assert warm_bundle["file_excerpts"].keys() == cold_bundle["file_excerpts"].keys()
    assert warm_bundle["pack"].relevant_paths == cold_bundle["pack"].relevant_paths
    assert warm_bundle["pack"].estimated_tokens == cold_bundle["pack"].estimated_tokens
