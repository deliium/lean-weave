from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from benchmarks.fixtures import load_manifest, materialize
from lean_weave.cli import app
from lean_weave.code_index import CodeIndex
from lean_weave.config.models import ContextConfig
from lean_weave.context.models import iter_lines
from lean_weave.context.provider import DeterministicContextProvider
from lean_weave.schemas import FeaturePlan, FeatureState, ImplementationTask, Stage

runner = CliRunner()


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


def test_bug_fix_map_ranges_survive_explicit_update(
    fixture_repo: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(fixture_repo)
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    assert runner.invoke(app, ["index", "build"]).exit_code == 0

    first = runner.invoke(app, ["map", "show"])
    second = runner.invoke(app, ["map", "show"])
    assert first.exit_code == 0 and second.exit_code == 0
    assert first.stdout == second.stdout
    assert "calc.add" in first.stdout

    module = runner.invoke(app, ["map", "module", "calc"])
    assert module.exit_code == 0
    assert "calc ::" in module.stdout

    refs = runner.invoke(app, ["map", "references", "calc.add"])
    assert refs.exit_code == 0
    index = CodeIndex(fixture_repo, cache_base=cache)
    hit = index.lookup("add", path="src/calc.py").hits[0]
    expected = f"src/calc.py:{hit.start_line}-{hit.end_line}"
    assert expected in refs.stdout
    source = (fixture_repo / "src" / "calc.py").read_text(encoding="utf-8")
    lines = iter_lines(source)
    assert "def add" in "\n".join(lines[hit.start_line - 1 : hit.end_line])

    (fixture_repo / "src" / "calc.py").write_text("\ndef add(a, b):\n    return a - b\n")
    stale = runner.invoke(app, ["map", "show"])
    assert stale.exit_code == 1
    assert stale.stdout == ""
    assert "Error: stale" in stale.stderr
    assert runner.invoke(app, ["index", "update"]).exit_code == 0
    updated = runner.invoke(app, ["map", "references", "calc.add"])
    assert updated.exit_code == 0
    new_hit = index.lookup("add", path="src/calc.py").hits[0]
    assert new_hit.start_line == hit.start_line + 1
    assert f"src/calc.py:{new_hit.start_line}-{new_hit.end_line}" in updated.stdout


def test_render_symbolref_and_multi_file_src_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    checkout = tmp_path / "render-checkout"
    models_src = Path("src/lean_weave/context/models.py")
    render_src = Path("src/lean_weave/context/render.py")
    dest_models = checkout / models_src
    dest_render = checkout / render_src
    dest_models.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(models_src, dest_models)
    shutil.copy2(render_src, dest_render)
    config_dir = checkout / ".lean-weave"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "version: 1\ncontext:\n  map_rows_max: 500\n  map_tokens_max: 8192\n",
        encoding="utf-8",
    )
    cache = tmp_path / "render-cache"
    cache.mkdir(mode=0o700)
    monkeypatch.chdir(checkout)
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))

    assert runner.invoke(app, ["index", "build"]).exit_code == 0
    deps = runner.invoke(app, ["map", "dependencies", "lean_weave.context.models"])
    assert deps.exit_code == 0, deps.output
    assert "in " in deps.stdout
    assert "render.py" in deps.stdout
    refs = runner.invoke(app, ["map", "references", "lean_weave.context.models.SymbolRef"])
    assert refs.exit_code == 0, refs.output
    assert "def " in refs.stdout
    assert "SymbolRef" in refs.stdout
    models_text = dest_models.read_text(encoding="utf-8")
    class_line = next(
        index
        for index, line in enumerate(iter_lines(models_text), start=1)
        if line.startswith("class SymbolRef")
    )
    assert f"models.py:{class_line}-" in refs.stdout

    preview = runner.invoke(
        app,
        [
            "context",
            "build",
            "preview",
            "--level",
            "map",
            "--indexed-map",
            "--path",
            "src/lean_weave/context/models.py",
            "--json",
        ],
    )
    assert preview.exit_code == 0, preview.output
    payload = json.loads(preview.stdout)
    assert payload["relevant_paths"] == ["src/lean_weave/context/models.py"]
    assert payload["excerpts"] == []

    multi = materialize("multi_file_feature", tmp_path / "multi").root
    multi_cache = tmp_path / "multi-cache"
    multi_cache.mkdir(mode=0o700)
    monkeypatch.chdir(multi)
    monkeypatch.setenv("XDG_CACHE_HOME", str(multi_cache))
    assert runner.invoke(app, ["index", "build"]).exit_code == 0
    module = runner.invoke(app, ["map", "module", "config"])
    assert module.exit_code == 0
    deps_app = runner.invoke(app, ["map", "dependencies", "app"])
    assert deps_app.exit_code == 0
    assert "src.config" in deps_app.stdout
    assert "unresolved(not_indexed)" in deps_app.stdout or "not_indexed" in deps_app.stdout
