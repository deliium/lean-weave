import json
import logging
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from lean_weave.cli import app, main
from lean_weave.cli.commands import doctor as doctor_module
from lean_weave.errors import LeanWeaveError

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LEAN_WEAVE_LOG_LEVEL", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


@pytest.mark.parametrize("args", [
    ["--help"], ["init", "--help"], ["config", "--help"],
    ["config", "show", "--help"], ["doctor", "--help"],
    ["feature", "--help"], ["feature"],
])
@pytest.mark.parametrize("source", ["missing", "malformed"])
def test_help_bypasses_config(args, source):
    path = Path("bad.yaml")
    if source == "malformed":
        path.write_text("llm: [")
    result = runner.invoke(app, ["--config", str(path), *args])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.stdout
    assert "Config.load" not in result.stderr


def test_init_create_refuse_force_directory():
    args = ["--config", "new/nested.yaml", "init"]
    assert runner.invoke(app, args).exit_code == 0
    assert runner.invoke(app, args).exit_code == 1
    Path("new/nested.yaml").write_text("llm: [")
    assert runner.invoke(app, [*args, "--force"]).exit_code == 0
    assert runner.invoke(app, ["--config", "new", "init"]).exit_code == 1


@pytest.mark.parametrize("fmt", ["yaml", "json"])
@pytest.mark.parametrize("env_name", ["DEEPSEEK_API_KEY", "CUSTOM_KEY"])
def test_config_output_redaction_and_stderr(monkeypatch, fmt, env_name):
    monkeypatch.setenv(env_name, "very-sensitive-key-value")
    Path("config.yaml").write_text(f"llm:\n  api_key_env: {env_name}\nlogging:\n  level: DEBUG\n")
    result = runner.invoke(app, ["--config", "config.yaml", "config", "show", "--format", fmt])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout) if fmt == "json" else yaml.safe_load(result.stdout)
    assert data["api_key_present"] is True
    assert data["llm"]["api_key_env"] == env_name
    assert "very-sensitive-key-value" not in result.output
    assert "[CLI.config.show]" in result.stderr


@pytest.mark.parametrize("source", ["missing", "invalid"])
def test_config_error_code(source):
    if source == "invalid":
        Path("config.yaml").write_text("version: 2")
    result = runner.invoke(app, ["--config", "config.yaml", "config", "show"])
    assert result.exit_code == 3
    assert "Traceback" not in result.output


@pytest.mark.parametrize("argv,code", [
    (["unknown-command"], 2),
    (["--config", "missing", "config", "show"], 3),
    (["--config", "missing", "doctor"], 3),
    (["feature"], 0), (["--help"], 0),
    (["config", "show", "--format", "unsupported"], 2),
])
def test_exported_main(argv, code, capsys):
    with pytest.raises(SystemExit) as error:
        main(argv)
    assert error.value.code == code
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("flag,lean_env,generic_env,yaml_level,expected", [
    ("ERROR", "DEBUG", "WARNING", "INFO", logging.ERROR),
    ("", "DEBUG", "WARNING", "DEBUG", logging.INFO),
    (None, "DEBUG", "WARNING", "INFO", logging.DEBUG),
    (None, None, "ERROR", "DEBUG", logging.ERROR),
    (None, None, None, "DEBUG", logging.DEBUG),
    (None, "bad-sensitive-value", None, "DEBUG", logging.INFO),
])
def test_log_precedence(monkeypatch, flag, lean_env, generic_env, yaml_level, expected):
    Path("config.yaml").write_text(f"logging:\n  level: {yaml_level}\n  format: json\n")
    if lean_env:
        monkeypatch.setenv("LEAN_WEAVE_LOG_LEVEL", lean_env)
    if generic_env:
        monkeypatch.setenv("LOG_LEVEL", generic_env)
    args = ["--config", "config.yaml"]
    if flag is not None:
        args += ["--log-level", flag]
    result = runner.invoke(app, [*args, "config", "show"])
    assert result.exit_code == 0, result.output
    assert logging.getLogger("lean_weave").level == expected
    assert yaml.safe_load(result.stdout)["logging"]["level"] == yaml_level
    assert "bad-sensitive-value" not in result.output
    for line in result.stderr.splitlines():
        json.loads(line)
    if expected < logging.ERROR:
        assert json.loads(result.stderr.splitlines()[-1])["level"] in {"INFO", "DEBUG", "WARNING"}


def test_missing_default_config_show():
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    assert yaml.safe_load(result.stdout)["api_key_present"] is False
    assert "defaults" in result.stderr


def test_log_format_flag_overrides_yaml():
    Path("config.yaml").write_text("logging: {format: json}")
    result = runner.invoke(app, ["--config", "config.yaml", "--log-format", "text", "config", "show"])
    assert result.exit_code == 0
    assert result.stderr.splitlines()[-1].startswith("INFO")


def test_unknown_command_does_not_leak_env_key(monkeypatch, capsys):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sensitive-command-value")
    with pytest.raises(SystemExit) as error:
        main(["sensitive-command-value"])
    assert error.value.code == 2
    assert "sensitive-command-value" not in capsys.readouterr().err


def test_config_error_path_redacts_current_key(monkeypatch, capsys):
    sentinel = "verification-dummy-credential"
    monkeypatch.setenv("DEEPSEEK_API_KEY", sentinel)
    args = ["--config", f"{sentinel}.yaml", "config", "show"]
    result = runner.invoke(app, args)
    assert result.exit_code == 3
    assert sentinel not in result.output
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 3
    assert sentinel not in capsys.readouterr().err


def test_deep_yaml_console_config_exit(capsys):
    Path("deep.yaml").write_text("llm: " + "[" * 1000 + "0" + "]" * 1000)
    with pytest.raises(SystemExit) as error:
        main(["--config", "deep.yaml", "config", "show"])
    assert error.value.code == 3
    assert "Traceback" not in capsys.readouterr().err


def test_custom_key_redacted_in_post_config_error(monkeypatch):
    sentinel = "custom-verification-dummy-credential"
    monkeypatch.setenv("CUSTOM_KEY", sentinel)
    Path("custom.yaml").write_text("llm: {api_key_env: CUSTOM_KEY}")
    monkeypatch.setattr(
        doctor_module, "check_writable",
        lambda path: (_ for _ in ()).throw(LeanWeaveError(f"Probe failed: {sentinel}")),
    )
    result = runner.invoke(app, ["--config", "custom.yaml", "doctor"])
    assert result.exit_code == 1
    assert sentinel not in result.output
    assert "[REDACTED]" in result.stderr
