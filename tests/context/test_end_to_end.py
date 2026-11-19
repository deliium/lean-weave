import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lean_weave.cli import app
from lean_weave.config import ContextConfig, init_config
from lean_weave.context.builder import ContextBuilder
from lean_weave.context.models import ContextQuery, ExpansionLevel, content_digest
from lean_weave.context.provider import DeterministicContextProvider
from lean_weave.context.repository import Repository
from lean_weave.schemas import FeatureState, Stage

from .conftest import write_files

ROOT = Path(__file__).parents[2]
runner = CliRunner()


def _pack(project: Path, task: str = "helper") -> str:
    repository = Repository(project, ContextConfig())
    result = ContextBuilder(repository).build(
        ContextQuery(task=task, level=ExpansionLevel.RANGES, paths=["pkg/core.py"])
    )
    return result.pack.model_dump_json()


def test_identical_inputs_produce_byte_identical_packs(project: Path):
    write_files(
        project,
        {
            "pkg/__init__.py": "",
            "pkg/core.py": "def helper():\n    return 1\n",
        },
    )
    assert _pack(project) == _pack(project)


def test_candidate_creation_order_does_not_change_pack(project: Path):
    files = {
        "a.py": "def alpha():\n    return 1\n",
        "b.py": "def beta():\n    return 2\n",
        "c.py": "def gamma():\n    return 3\n",
    }
    first = project / "first"
    second = project / "second"
    write_files(first, dict(reversed(list(files.items()))))
    write_files(second, files)
    query = ContextQuery(task="alpha beta gamma", level=ExpansionLevel.SYMBOLS)
    first_pack = ContextBuilder(Repository(first)).build(query).pack.model_dump_json()
    second_pack = ContextBuilder(Repository(second)).build(query).pack.model_dump_json()
    assert first_pack == second_pack


def test_real_project_symbol_and_line_coordinates():
    assert (ROOT / "src" / "lean_weave" / "app.py").exists()
    repository = Repository(ROOT)
    result = ContextBuilder(repository).build(
        ContextQuery(
            task="create_llm_provider",
            level=ExpansionLevel.RANGES,
            paths=["src/lean_weave/app.py"],
            symbols=["create_llm_provider"],
            exact_symbols_only=True,
            scope_paths=["src/lean_weave/app.py"],
        )
    )
    symbol = next(
        item for item in result.pack.symbols if item.simple_name == "create_llm_provider"
    )
    source = (ROOT / "src" / "lean_weave" / "app.py").read_text()
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    assert lines[symbol.start_line - 1].startswith("def create_llm_provider")
    assert "journal" in (symbol.signature or "")
    excerpt = next(item for item in result.pack.excerpts if item.symbol == symbol.qualified_name)
    assert excerpt.text == "\n".join(lines[excerpt.start_line - 1 : excerpt.end_line])
    assert excerpt.digest == content_digest("\n".join(lines))


def test_real_project_through_cli_with_generated_config(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    config = tmp_path / "config.yaml"
    init_config(config)
    result = runner.invoke(
        app,
        [
            "--config",
            str(config),
            "context",
            "build",
            "create_llm_provider",
            "--path",
            "src/lean_weave/app.py",
            "--level",
            "ranges",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    excerpt = next(item for item in data["excerpts"] if item["symbol"] == "lean_weave.app.create_llm_provider")
    source = (ROOT / "src" / "lean_weave" / "app.py").read_text()
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    assert excerpt["text"] == "\n".join(
        lines[excerpt["start_line"] - 1 : excerpt["end_line"]]
    )
    assert excerpt["digest"] == content_digest("\n".join(lines))


def test_plan_stage_has_no_network_or_subprocess(project: Path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("no subprocess or provider call is allowed")

    monkeypatch.setattr("subprocess.run", forbidden)
    monkeypatch.setattr("lean_weave.context.provider.DeterministicContextProvider._repository", forbidden)
    provider = DeterministicContextProvider(project)
    state = FeatureState(feature_id="f", stage=Stage.PLAN, request="Plan only")
    bundle = provider.build("plan", state)
    assert bundle["file_excerpts"] == {}
    assert bundle["estimated_tokens"] == bundle["pack"].estimated_tokens
