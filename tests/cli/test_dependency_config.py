"""CLI projection of opt-in dependency coverage settings and packed diagnostics."""

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
def project(tmp_path: Path) -> Path:
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "__init__.py").write_text("")
    (tmp_path / "models" / "widget.py").write_text(
        "class HarborWidget:\n    def spin(self):\n        return 1\n"
    )
    (tmp_path / "service").mkdir()
    (tmp_path / "service" / "__init__.py").write_text("")
    (tmp_path / "service" / "runner.py").write_text(
        "from models.widget import HarborWidget\n\n"
        "def activate(item: HarborWidget):\n    return item.spin()\n"
    )
    return tmp_path


def invoke(*args):
    base = ["--config", "config.yaml"] if Path("config.yaml").exists() else []
    return runner.invoke(app, [*base, *args])


def _write_enabled_config(path: Path) -> None:
    runner.invoke(app, ["--config", "config.yaml", "init"])
    data = yaml.safe_load(path.read_text())
    data["context"]["dependency_coverage"] = {
        "enabled": True,
        "max_depth": 1,
        "max_definitions": 4,
        "max_tokens": 512,
    }
    path.write_text(yaml.safe_dump(data))


@pytest.mark.parametrize("fmt", ["yaml", "json"])
def test_config_show_exposes_dependency_coverage_settings(project: Path, fmt: str):
    _write_enabled_config(project / "config.yaml")
    result = invoke("config", "show", "--format", fmt)
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout) if fmt == "json" else yaml.safe_load(result.stdout)
    coverage = data["context"]["dependency_coverage"]
    assert coverage == {
        "enabled": True,
        "max_depth": 1,
        "max_definitions": 4,
        "max_tokens": 512,
    }
    assert set(coverage) == {"enabled", "max_depth", "max_definitions", "max_tokens"}


def test_disabled_packed_payload_omits_dependency_fields(project: Path):
    result = invoke("context", "build", "activate HarborWidget", "--packed", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["status"] == "ready"
    assert "dependency_coverage" not in data
    assert "dependency_evidence" not in data
    assert "dependency_issues" not in data


def test_enabled_build_and_inspect_project_dependency_diagnostics(project: Path):
    _write_enabled_config(project / "config.yaml")
    built = invoke("context", "build", "activate HarborWidget", "--packed", "--json")
    assert built.exit_code == 0, built.output
    data = json.loads(built.stdout)
    assert data["status"] == "ready"
    assert data["dependency_coverage"]["enabled"] is True
    assert data["dependency_coverage"]["definitions_added"] >= 1
    assert any(
        item["relationship"] == "imported_definition"
        for item in data["dependency_evidence"]
    )
    assert any(fragment["path"] == "models/widget.py" for fragment in data["fragments"])
    assert "text" in data["fragments"][0]

    inspected = invoke(
        "context", "inspect", "activate HarborWidget", "--packed", "--json"
    )
    assert inspected.exit_code == 0, inspected.output
    inspected_data = json.loads(inspected.stdout)
    assert inspected_data["dependency_coverage"]["enabled"] is True
    assert "dependency_evidence" in inspected_data
    assert "text" not in inspected_data["fragments"][0]
    for row in inspected_data["dependency_evidence"]:
        assert "text" not in row


def test_enabled_budget_stays_numeric_without_paths(project: Path):
    _write_enabled_config(project / "config.yaml")
    result = invoke("context", "budget", "activate HarborWidget", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert "composition" in data
    assert "dependency_coverage" not in data
    assert "dependency_evidence" not in data
    assert "models/widget.py" not in result.stdout
    assert "service/runner.py" not in result.stdout


def test_enabled_human_build_shows_dependency_summary(project: Path):
    _write_enabled_config(project / "config.yaml")
    result = invoke("context", "build", "activate HarborWidget", "--packed")
    assert result.exit_code == 0, result.output
    assert "dependency=status=" in result.stdout
    assert "defs=" in result.stdout
