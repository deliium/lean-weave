from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lean_weave.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    cache = tmp_path.parent / f"{tmp_path.name}-cache"
    cache.mkdir(mode=0o700)
    os.chmod(cache, 0o700)
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.delenv("LEAN_WEAVE_LOG_LEVEL", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "core.py").write_text("def helper():\n    return 1\n")
    (tmp_path / "app.py").write_text("from pkg.core import helper\n")
    return tmp_path


def invoke(*args):
    return runner.invoke(app, list(args))


def test_map_help_is_lazy(tmp_path: Path):
    (tmp_path / "bad.yaml").write_text("llm: [")
    result = runner.invoke(app, ["--config", "bad.yaml", "map", "--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.stdout
    assert "show" in result.stdout
    assert "Config.load" not in result.stderr


def test_missing_index_refuses_without_stdout(isolated: Path):
    result = invoke("map", "show")
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Error: missing" in result.stderr
    assert "hint: run 'wl index build'" in result.stderr


def test_show_module_dependencies_references(isolated: Path, monkeypatch):
    assert invoke("index", "build").exit_code == 0

    def fail_gateway(*_args, **_kwargs):
        raise AssertionError("map commands must not construct an LLM gateway")

    monkeypatch.setattr("lean_weave.app.create_llm_provider", fail_gateway)

    shown = invoke("map", "show")
    assert shown.exit_code == 0, shown.output
    assert "pkg.core" in shown.stdout
    assert shown.stdout.endswith("\n") or shown.stdout == "." or shown.stdout == ""

    module = invoke("map", "module", "pkg.core")
    assert module.exit_code == 0
    assert "pkg.core ::" in module.stdout

    deps = invoke("map", "dependencies", "app")
    assert deps.exit_code == 0
    assert "out " in deps.stdout

    refs = invoke("map", "references", "helper")
    assert refs.exit_code == 0
    assert "def " in refs.stdout
    assert "import " in refs.stdout

    qualified = invoke("map", "references", "pkg.core.helper")
    assert qualified.exit_code == 0
    assert "pkg.core.helper" in qualified.stdout


def test_invalid_unknown_ambiguous_selectors(isolated: Path):
    assert invoke("index", "build").exit_code == 0
    invalid = invoke("map", "module", "class")
    assert invalid.exit_code == 2
    assert invalid.stdout == ""
    assert "Error: invalid_selector" in invalid.stderr

    missing = invoke("map", "module", "nope")
    assert missing.exit_code == 1
    assert missing.stdout == ""
    assert "Error: not_found" in missing.stderr

    (isolated / "pkg" / "core.pyi").write_text("def helper() -> int: ...\n")
    assert invoke("index", "update").exit_code == 0
    ambiguous = invoke("map", "module", "pkg.core")
    assert ambiguous.exit_code == 1
    assert "Error: ambiguous_selector" in ambiguous.stderr


def test_stale_refuses_and_update_recovers(isolated: Path):
    assert invoke("index", "build").exit_code == 0
    before = invoke("map", "show")
    assert before.exit_code == 0
    (isolated / "pkg" / "core.py").write_text("\ndef helper():\n    return 2\n")
    stale = invoke("map", "show")
    assert stale.exit_code == 1
    assert stale.stdout == ""
    assert "Error: stale" in stale.stderr
    assert "hint: run 'wl index update'" in stale.stderr
    assert invoke("index", "update").exit_code == 0
    after = invoke("map", "show")
    assert after.exit_code == 0
    assert after.stdout != before.stdout


def test_empty_index_show_is_empty(tmp_path: Path, monkeypatch):
    empty = tmp_path / "empty-root"
    empty.mkdir()
    monkeypatch.chdir(empty)
    cache = tmp_path / "empty-cache"
    cache.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    assert invoke("index", "build").exit_code == 0
    shown = invoke("map", "show")
    assert shown.exit_code == 0
    assert shown.stdout == ""
    missing = invoke("map", "module", "pkg")
    assert missing.exit_code == 1
    assert "Error: not_found" in missing.stderr
