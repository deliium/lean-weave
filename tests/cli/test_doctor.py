from pathlib import Path

import pytest
from typer.testing import CliRunner

from lean_weave.cli import app
from lean_weave.cli.commands import doctor as doctor_module

runner = CliRunner()


@pytest.mark.parametrize("state,existing", [
    (".lean-weave", False), ("custom", True), ("nested/state", False),
])
def test_doctor_state_paths(tmp_path, monkeypatch, state, existing):
    monkeypatch.chdir(tmp_path)
    Path("config.yaml").write_text(f"workflow:\n  state_dir: {state}\n")
    if existing:
        Path(state).mkdir()
    before = set(tmp_path.rglob("*"))
    result = runner.invoke(app, ["--config", "config.yaml", "doctor"])
    assert result.exit_code == 0, result.output
    assert str(tmp_path / state) in result.stdout
    assert set(tmp_path.rglob("*")) == before


def test_doctor_file_and_permission_failures(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path(".lean-weave").write_text("file")
    Path("config.yaml").write_text("{}")
    assert runner.invoke(app, ["--config", "config.yaml", "doctor"]).exit_code == 1
    Path(".lean-weave").unlink()
    monkeypatch.setattr(doctor_module.tempfile, "NamedTemporaryFile",
                        lambda **kwargs: (_ for _ in ()).throw(PermissionError()))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output


def test_probe_cleanup_on_write_failure(tmp_path, monkeypatch):
    probe = tmp_path / "probe"

    class BrokenProbe:
        name = str(probe)

        def __enter__(self):
            probe.touch()
            return self

        def __exit__(self, *args):
            return False

        def write(self, content):
            raise OSError()

    monkeypatch.setattr(doctor_module.tempfile, "NamedTemporaryFile", lambda **kwargs: BrokenProbe())
    with pytest.raises(Exception, match="not writable"):
        doctor_module.check_writable(tmp_path)
    assert not probe.exists()


def test_doctor_optional_checks_and_version_fallback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor_module, "version",
                        lambda name: (_ for _ in ()).throw(doctor_module.PackageNotFoundError()))
    monkeypatch.setenv("CUSTOM_KEY", "secret-value")
    Path("config.yaml").write_text("llm: {api_key_env: CUSTOM_KEY}")
    result = runner.invoke(app, ["--config", "config.yaml", "doctor"])
    assert result.exit_code == 0, result.output
    assert "git absent" in result.stdout
    assert "API key present" in result.stdout
    assert "secret-value" not in result.output
    monkeypatch.setattr(doctor_module.sys, "version_info", (3, 11))
    assert runner.invoke(app, ["doctor"]).exit_code == 1
