from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from lean_weave.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    (tmp_path / "home").mkdir()
    (tmp_path / "xdg-cache").mkdir()


@pytest.fixture
def files(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "core.py").write_text(
        "class Widget:\n"
        "    def render(self, value: str = 'x') -> str:\n"
        "        return value\n\n"
        "def helper(a, /, b, **kwargs):\n"
        "    return a + b\n"
    )
    (tmp_path / "tests" / "test_core.py").write_text(
        "from pkg.core import helper\n\ndef test_helper():\n    assert helper(1, 2) == 3\n"
    )
    return tmp_path


def invoke(*args):
    base = ["--config", "config.yaml"] if Path("config.yaml").exists() else []
    return runner.invoke(app, [*base, *args])


def test_default_build_stays_legacy(files: Path):
    result = invoke("context", "build", "helper", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert "schema_version" not in data
    assert data["command"] == "build"
    assert data["level"] == "symbols"


def test_explicit_packed_build_and_inspect(files: Path):
    built = invoke("context", "build", "helper", "--packed", "--json")
    assert built.exit_code == 0, built.output
    data = json.loads(built.stdout)
    assert data["schema_version"] == 2
    assert data["command"] == "build"
    assert data["status"] == "ready"
    assert data["fragments"]
    assert "text" in data["fragments"][0]

    inspected = invoke("context", "inspect", "helper", "--packed", "--json")
    assert inspected.exit_code == 0, inspected.output
    inspected_data = json.loads(inspected.stdout)
    assert inspected_data["status"] == "ready"
    assert "text" not in inspected_data["fragments"][0]
    assert inspected_data["fragments"][0]["fragment_id"]
    assert inspected_data["fragments"][0]["reasons"]


def test_budget_view_is_numeric_only(files: Path):
    result = invoke("context", "budget", "helper", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["command"] == "budget"
    assert data["schema_version"] == 2
    assert "composition" in data
    assert "pack" not in data
    assert "fragments" not in data
    assert "map_rows" not in data
    assert "fragments" not in data and "pkg/core.py" not in result.stdout
    assert "model" not in data
    assert data["composition"]["output_tokens"] > 0


def test_config_opt_in_makes_bare_build_packed(files: Path):
    runner.invoke(app, ["--config", "config.yaml", "init"])
    config_path = files / "config.yaml"
    data = yaml.safe_load(config_path.read_text())
    data["context"]["packing"]["cli_default"] = True
    config_path.write_text(yaml.safe_dump(data))
    result = invoke("context", "build", "helper", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 2
    legacy = invoke("context", "build", "helper", "--legacy", "--json")
    assert legacy.exit_code == 0, legacy.output
    assert "schema_version" not in json.loads(legacy.stdout)


@pytest.mark.parametrize(
    "args",
    [
        ("context", "build", "helper", "--packed", "--legacy"),
        ("context", "build", "helper", "--packed", "--level", "symbols"),
        ("context", "inspect", "helper", "--packed", "--symbol", "helper"),
    ],
)
def test_conflicting_mode_signals_exit_two(files: Path, args):
    result = invoke(*args)
    assert result.exit_code == 2


def test_packed_insufficient_is_successful_evaluation(files: Path):
    result = invoke("context", "build", "zzzz-unmatchable", "--packed", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["status"] == "insufficient_context"
    assert "no_relevant_source" in data["reason_codes"]


def test_legacy_inspect_remains_available(files: Path):
    result = invoke("context", "inspect", "pkg/core.py", "--legacy", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["mode"] == "metadata"
    assert data["path"] == "pkg/core.py"


def test_packed_views_share_numeric_accounting(files: Path):
    build = json.loads(invoke("context", "build", "helper", "--packed", "--json").stdout)
    inspect = json.loads(
        invoke("context", "inspect", "helper", "--packed", "--json").stdout
    )
    budget = json.loads(invoke("context", "budget", "helper", "--json").stdout)
    assert build["composition"] == inspect["composition"] == budget["composition"]


def test_packing_never_writes_the_code_index(files: Path):
    result = invoke("context", "build", "helper", "--packed", "--json")
    assert result.exit_code == 0, result.output
    cache = files / "xdg-cache"
    files_written = [path for path in cache.rglob("*") if path.is_file()]
    assert files_written == []


def test_tracked_artifact_provenance_and_view_parity(files: Path):
    source = (
        Path(__file__).resolve().parents[2]
        / "src/lean_weave/context/python_index.py"
    )
    target = files / "src/lean_weave/context/python_index.py"
    target.parent.mkdir(parents=True)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    build = invoke(
        "context", "build", "extract_python_facts", "--packed", "--json"
    )
    assert build.exit_code == 0, build.output
    built = json.loads(build.stdout)
    assert built["status"] == "ready"
    fragment = next(
        item for item in built["fragments"] if item["path"] == "src/lean_weave/context/python_index.py"
    )
    assert "def extract_python_facts" in fragment["text"]

    inspect = json.loads(
        invoke("context", "inspect", "extract_python_facts", "--packed", "--json").stdout
    )
    provenance = next(
        item
        for item in inspect["fragments"]
        if item["path"] == "src/lean_weave/context/python_index.py"
    )
    assert provenance["digest"] == fragment["digest"]
    assert provenance["start_line"] == fragment["start_line"]

    budget = json.loads(
        invoke("context", "budget", "extract_python_facts", "--json").stdout
    )
    assert budget["composition"]["input_tokens"] == built["composition"]["input_tokens"]
    assert "src/lean_weave/context/python_index.py" not in json.dumps(budget)
