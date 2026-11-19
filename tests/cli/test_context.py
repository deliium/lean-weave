import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from lean_weave.cli import app, main

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LEAN_WEAVE_LOG_LEVEL", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


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


def test_context_help_bypasses_config(tmp_path):
    (tmp_path / "bad.yaml").write_text("llm: [")
    result = runner.invoke(app, ["--config", "bad.yaml", "context", "--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.stdout
    assert "Config.load" not in result.stderr


def test_init_generates_context_settings(files):
    result = runner.invoke(app, ["--config", "config.yaml", "init"])
    assert result.exit_code == 0
    data = yaml.safe_load((files / "config.yaml").read_text())
    assert data["context"]["stage_prompt_tokens_max"] == 6000
    assert data["context"]["exclusions"] == []


def test_config_show_hides_exclusion_patterns(files, monkeypatch):
    sentinel = "sk-" + "a" * 32
    (files / "config.yaml").write_text(
        "context:\n  exclusions:\n    - " + sentinel + "\n"
    )
    for fmt in ("yaml", "json"):
        result = invoke("config", "show", "--format", fmt)
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout) if fmt == "json" else yaml.safe_load(result.stdout)
        assert data["context"]["exclusion_count"] == 1
        assert "exclusions" not in data["context"]
        assert sentinel not in result.output
        assert sentinel not in result.stderr


def test_search_literal_json_and_zero_hits(files):
    result = invoke("context", "search", "helper", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["mode"] == "literal"
    assert data["hits"]
    assert data["hits"][0]["path"] == "pkg/core.py"

    empty = invoke("context", "search", "absent-token", "--json")
    assert empty.exit_code == 0
    assert json.loads(empty.stdout)["hits"] == []


def test_search_symbol_and_limit_validation(files):
    result = invoke("context", "search", "helper", "--symbol", "helper", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["mode"] == "symbol"
    assert data["hits"][0]["name"] == "helper"

    for limit in ("0", "101"):
        invalid = invoke("context", "search", "helper", "--limit", limit)
        assert invalid.exit_code == 2


def test_search_forbidden_path(files):
    result = invoke("context", "search", "helper", "--path", "missing.py")
    assert result.exit_code == 1
    assert "not available" in result.stderr


def test_build_symbols_and_provenance(files):
    result = invoke("context", "build", "helper", "--path", "pkg/core.py", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["level"] == "symbols"
    assert data["estimated_tokens"] > 0
    symbol = next(item for item in data["symbols"] if item["simple_name"] == "helper")
    assert symbol["signature"].startswith("def helper")
    assert data["excerpts"] == []


def test_build_full_requires_path_and_reason(files):
    missing_reason = invoke("context", "build", "core", "--level", "full", "--path", "pkg/core.py")
    assert missing_reason.exit_code == 2
    missing_path = invoke("context", "build", "core", "--level", "full", "--reason", "why")
    assert missing_path.exit_code == 2
    two_paths = invoke(
        "context",
        "build",
        "core",
        "--level",
        "full",
        "--path",
        "pkg/core.py",
        "--path",
        "pkg/__init__.py",
        "--reason",
        "why",
    )
    assert two_paths.exit_code == 2
    ok = invoke(
        "context",
        "build",
        "core",
        "--level",
        "full",
        "--path",
        "pkg/core.py",
        "--reason",
        "whole file",
        "--json",
    )
    assert ok.exit_code == 0, ok.output
    assert json.loads(ok.stdout)["excerpts"][0]["kind"] == "full"


def test_build_invalid_level_and_forbidden_path(files):
    assert invoke("context", "build", "core", "--level", "nope").exit_code == 2
    assert invoke("context", "build", "core", "--path", "missing.py").exit_code == 1


def test_build_budget_failure_is_operational(files):
    (files / "config.yaml").write_text("context:\n  stage_prompt_tokens_max: 10\n")
    result = invoke("context", "build", "core")
    assert result.exit_code == 1
    assert "budget" in result.stderr.lower()
    assert "Traceback" not in result.output


def test_configuration_error_exit_code(files):
    (files / "bad.yaml").write_text("context: {stage_prompt_tokens_max: 0}\n")
    result = runner.invoke(app, ["--config", "bad.yaml", "context", "build", "core"])
    assert result.exit_code == 3


def test_inspect_metadata_symbol_and_lines(files):
    metadata = invoke("context", "inspect", "pkg/core.py", "--json")
    assert metadata.exit_code == 0, metadata.output
    data = json.loads(metadata.stdout)
    assert data["mode"] == "metadata"
    assert data["digest"].startswith("sha256:")
    assert any(item["simple_name"] == "helper" for item in data["symbols"])

    symbol = invoke("context", "inspect", "pkg/core.py", "--symbol", "helper", "--json")
    assert symbol.exit_code == 0, symbol.output
    pack = json.loads(symbol.stdout)["pack"]
    assert pack["excerpts"][0]["symbol"] == "pkg.core.helper"

    lines = invoke("context", "inspect", "pkg/core.py", "--lines", "5:6", "--json")
    assert lines.exit_code == 0, lines.output
    excerpt = json.loads(lines.stdout)["pack"]["excerpts"][0]
    assert excerpt["kind"] == "partial"
    assert excerpt["text"].startswith("def helper")


def test_inspect_usage_and_forbidden(files):
    assert invoke("context", "inspect", "pkg/core.py", "--symbol", "helper", "--lines", "1:2").exit_code == 2
    assert invoke("context", "inspect", "pkg/core.py", "--lines", "2:1").exit_code == 2
    assert invoke("context", "inspect", "pkg/core.py", "--lines", "nope").exit_code == 2
    assert invoke("context", "inspect", "missing.py").exit_code == 1
    assert invoke("context", "inspect", "pkg/core.py", "--symbol", "absent").exit_code == 2


def test_human_output_escapes_terminal_controls(files):
    (files / "pkg" / "control.py").write_text("VALUE = '\x1b[31mred\x1b[0m'\n")
    result = invoke("context", "search", "red")
    assert result.exit_code == 0
    assert "\x1b" not in result.stdout
    assert "\\x1b" in result.stdout


def test_secret_canary_never_reaches_output_or_logs(files, caplog):
    canary = "sk-" + "b" * 32
    (files / "pkg" / "leak.py").write_text(f"KEY = '{canary}'\n")
    result = invoke("context", "build", "leak")
    assert result.exit_code == 0, result.output
    assert canary not in result.output
    assert canary not in result.stderr
    assert canary not in caplog.text


def test_exported_main_context_search(files, capsys):
    with pytest.raises(SystemExit) as error:
        main(["context", "search", "helper", "--json"])
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert json.loads(output)["hits"]
