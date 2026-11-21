"""CLI task-aware retrieval commands."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from benchmarks.fixtures import materialize
from benchmarks.offline import installed_scripts_dir, run_installed
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
    (tmp_path / "pkg" / "auth.py").write_text("def login():\n    return 1\n")
    return tmp_path


def invoke(*args):
    base = ["--config", "config.yaml"] if Path("config.yaml").exists() else []
    return runner.invoke(app, [*base, *args])


def test_rank_explain_and_task_aware_search(files):
    runner.invoke(app, ["--config", "config.yaml", "init"])
    for verb in ("rank", "explain"):
        result = invoke("context", verb, "login", "--task-kind", "bugfix", "--json")
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["command"] == verb
        assert data["task_kind"] == "bugfix"
        assert "hits" in data
        assert "degraded_reasons" in data

    result = invoke("context", "search", "login", "--task-aware", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["command"] == "search"
    assert data["mode"] == "task-aware"
    assert any(hit["path"] == "pkg/auth.py" for hit in data["hits"])


def test_rejects_mixed_selectors(files):
    runner.invoke(app, ["--config", "config.yaml", "init"])
    result = invoke("context", "search", "login", "--task-aware", "--path", "pkg/auth.py")
    assert result.exit_code != 0
    result = invoke("context", "search", "login", "--task-kind", "bugfix")
    assert result.exit_code != 0


def test_literal_search_unchanged(files):
    runner.invoke(app, ["--config", "config.yaml", "init"])
    result = invoke("context", "search", "login", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["mode"] == "literal"
    assert data["command"] == "search"


def test_init_includes_retrieval_defaults(files):
    result = runner.invoke(app, ["--config", "config.yaml", "init"])
    assert result.exit_code == 0
    data = yaml.safe_load((files / "config.yaml").read_text())
    assert data["context"]["retrieval"]["top_k"] == 10
    assert data["context"]["retrieval"]["weights"]["exact_symbol"] == 36


def test_help_lists_new_commands(files):
    result = runner.invoke(app, ["context", "--help"])
    assert result.exit_code == 0
    assert "rank" in result.stdout
    assert "explain" in result.stdout


def test_installed_entry_points_rank_bug_fix_parity(tmp_path: Path):
    """Both installed spellings agree on pinned bug_fix rank --json."""

    scripts = installed_scripts_dir()
    assert (scripts / "wl").is_file()
    assert (scripts / "lean-weave").is_file()

    root = materialize("bug_fix", tmp_path / "repo").root
    # Autouse fixture already isolates HOME/XDG; keep explicit env for run_installed.
    home = tmp_path / "home"
    xdg = tmp_path / "xdg-cache"
    env = {
        "HOME": str(home),
        "XDG_CACHE_HOME": str(xdg),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C",
        "LC_ALL": "C",
    }
    assert "DEEPSEEK_API_KEY" not in env

    init = run_installed("init", cwd=root, env=env)
    assert init.returncode == 0, init.stdout + init.stderr
    config_path = Path(init.stdout.strip())
    assert config_path.is_file()

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["context"]["retrieval"]["top_k"] = 7
    config_path.write_text(
        "# lean-weave product configuration\n" + yaml.safe_dump(data, sort_keys=False),
        encoding="utf-8",
    )

    payloads = []
    for entry in ("wl", "lean-weave"):
        result = run_installed(
            "--config",
            str(config_path),
            "context",
            "rank",
            "fix add returns wrong result",
            "--task-kind",
            "bugfix",
            "--json",
            cwd=root,
            env=env,
            entry=entry,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        payloads.append(payload)

    assert payloads[0] == payloads[1]
    payload = payloads[0]
    assert payload["command"] == "rank"
    assert payload["task_kind"] == "bugfix"
    assert payload["top_k"] == 7
    assert any(hit["path"] == "src/calc.py" for hit in payload["hits"])
    assert any(
        hit["path"] == "src/calc.py"
        and hit.get("qualified_name") == "calc.add"
        for hit in payload["hits"]
    ) or any(
        hit["path"] == "src/calc.py" and hit.get("kind") == "file"
        for hit in payload["hits"]
    )
