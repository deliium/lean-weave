from __future__ import annotations

import os
import sqlite3
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
    (tmp_path / "pkg" / "core.py").write_text("def helper():\n    return 1\n")
    return tmp_path


def invoke(*args):
    return runner.invoke(app, list(args))


def test_index_help_is_offline_even_with_invalid_config(tmp_path: Path):
    (tmp_path / "bad.yaml").write_text("llm: [")
    result = runner.invoke(app, ["--config", "bad.yaml", "index", "--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.stdout
    assert "Config.load" not in result.stderr


def test_status_missing_then_build_and_update(isolated: Path):
    missing = invoke("index", "status")
    assert missing.exit_code == 0
    assert missing.stdout.startswith("index health=missing freshness=unknown")

    refused = invoke("index", "update")
    assert refused.exit_code == 1
    assert "Error: missing" in refused.stderr
    assert "hint: run 'wl index build'" in refused.stderr

    built = invoke("index", "build")
    assert built.exit_code == 0, built.output
    assert built.stdout.startswith("index built revision=sha256:")
    assert " schema=1 " in built.stdout
    for key in (
        "added=", "modified=", "moved=", "removed=", "unchanged=",
        "invalidated=", "parsed=", "syntax_failed=",
    ):
        assert key in built.stdout

    again = invoke("index", "update")
    assert again.exit_code == 0
    assert "unchanged=1" in again.stdout
    assert "parsed=0" in again.stdout


def test_status_tracks_staleness_and_corruption(isolated: Path):
    assert invoke("index", "build").exit_code == 0
    (isolated / "pkg" / "core.py").write_text("def helper():\n    return 2\n")
    stale = invoke("index", "status")
    assert stale.exit_code == 0
    assert "health=healthy freshness=stale" in stale.stdout
    assert invoke("index", "update").exit_code == 0
    current = invoke("index", "status")
    assert "health=healthy freshness=current" in current.stdout

    cache = isolated.parent / f"{isolated.name}-cache"
    database = next(cache.rglob("index.sqlite3"))
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA application_id=1")
    connection.commit()
    connection.close()
    broken = invoke("index", "status")
    assert broken.exit_code == 1
    assert "health=incompatible" in broken.stdout
    assert "hint: run 'wl index clear --yes'" in broken.stderr


def test_clear_requires_confirmation_and_is_idempotent(isolated: Path):
    assert invoke("index", "build").exit_code == 0
    refused = invoke("index", "clear")
    assert refused.exit_code == 2
    assert "requires --yes" in refused.stderr
    assert list((isolated.parent / f"{isolated.name}-cache").rglob("index.sqlite3"))

    cleared = invoke("index", "clear", "--yes")
    assert cleared.exit_code == 0
    assert cleared.stdout.strip() == "cleared"
    again = invoke("index", "clear", "--yes")
    assert again.exit_code == 0
    assert again.stdout.strip() == "missing"


def test_indexing_is_offline_and_leaves_no_ledger(isolated: Path, monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("index commands must not construct an LLM provider")

    monkeypatch.setattr("lean_weave.app.create_llm_provider", fail)
    assert "DEEPSEEK_API_KEY" not in os.environ
    assert invoke("index", "build").exit_code == 0
    assert invoke("index", "status").exit_code == 0
    assert invoke("index", "update").exit_code == 0
    assert not (isolated / ".lean-weave").exists()
