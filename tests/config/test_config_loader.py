from pathlib import Path

import pytest
import yaml

from lean_weave.config import ConfigError, dump_config, init_config, load_config
from lean_weave.errors import LeanWeaveError


@pytest.fixture(autouse=True)
def project_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def test_missing_default_and_explicit():
    loaded = load_config()
    assert loaded.source is None and loaded.warnings
    with pytest.raises(ConfigError):
        load_config(Path("missing"), required=True)


def test_default_file_and_ai_factory_ignored():
    Path(".ai-factory").mkdir()
    Path(".ai-factory/config.yaml").write_text("invalid: ignored")
    assert load_config().source is None
    path = init_config()
    assert load_config().source == path
    assert yaml.safe_load(dump_config(load_config().config))["version"] == 1


def test_init_create_refuse_force_and_directory():
    path = init_config(Path("nested/config.yaml"))
    with pytest.raises(LeanWeaveError):
        init_config(path)
    path.write_text("broken")
    init_config(path, force=True)
    assert load_config(path, required=True).config.version == 1
    with pytest.raises(LeanWeaveError):
        init_config(path.parent, force=True)


@pytest.mark.parametrize("text", [
    "llm: [", "!!python/object/apply:os.system ['secret-looking-value']",
    "version: 2", "workflow: {state_dir: ../secret-looking-value}",
    "llm: {api_key: secret-looking-value}", "secret-looking-value: 1",
    "[secret-looking-value]", "llm: {api_key_env: secret-looking-value}",
    "llm: {thinking: secret-looking-value}", "",
])
def test_safe_errors(text, caplog):
    path = Path("bad.yaml")
    path.write_text(text)
    with pytest.raises(ConfigError) as error:
        load_config(path, required=True)
    assert "secret-looking-value" not in str(error.value) + caplog.text


def test_state_symlink_escape(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    Path(".lean-weave").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ConfigError, match="state_dir"):
        load_config()


def test_unreadable_and_undecodable(monkeypatch):
    path = Path("bad.yaml")
    path.write_bytes(b"\xff")
    with pytest.raises(ConfigError, match="Cannot read"):
        load_config(path)
    with pytest.raises(ConfigError, match="Cannot read"):
        load_config(Path.cwd())
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(ConfigError, match="Cannot read"):
        load_config(path)


def test_resolution_failure(monkeypatch):
    monkeypatch.setattr(Path, "resolve", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    with pytest.raises(ConfigError, match="resolve"):
        load_config()


def test_deep_yaml_is_safe_config_error():
    path = Path("deep.yaml")
    path.write_text("llm: " + "[" * 1000 + "0" + "]" * 1000)
    with pytest.raises(ConfigError, match="YAML"):
        load_config(path, required=True)


def test_yaml_recursion_diagnostics_never_echo_payload(monkeypatch, caplog):
    Path("deep.yaml").write_text("{}")
    monkeypatch.setattr(
        yaml, "safe_load",
        lambda text: (_ for _ in ()).throw(RecursionError("secret-looking-value")),
    )
    with pytest.raises(ConfigError) as error:
        load_config(Path("deep.yaml"), required=True)
    assert "secret-looking-value" not in str(error.value) + caplog.text
