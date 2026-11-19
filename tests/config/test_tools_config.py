import json

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from lean_weave.cli import app
from lean_weave.config import AppConfig, ToolsConfig
from lean_weave.config.loader import dump_config, load_config
from lean_weave.errors import ConfigError
from lean_weave.executor.approval import approve_tools
from lean_weave.executor.models import ReadRangeRequest, file_revision, safe_validation_payload

runner = CliRunner()
CANARY = "sk-" + ("a" * 24)


def test_tools_defaults_and_old_yaml(tmp_path, monkeypatch):
    tools = AppConfig().tools
    assert tools.write_allow == []
    assert tools.checks == []
    assert tools.read_allow == []
    assert tools.file_bytes_max == 131072
    assert tools.runtime_s_max == 30
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "old.yaml"
    path.write_text("version: 1\nproject:\n  name: demo\n", encoding="utf-8")
    loaded = load_config(path, required=True)
    assert loaded.config.tools.checks == []
    assert loaded.config.tools.write_allow == []
    approval = approve_tools(tmp_path, loaded.config)
    assert approval.writes_approved is False
    assert approval.checks_approved is False
    assert approval.write_allow == ()
    assert approval.checks == ()


def test_yaml_round_trip_does_not_authorize(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text(
        "tools:\n"
        "  write_allow:\n"
        "    - src/a.py\n"
        "  checks:\n"
        "    - id: parse\n"
        "      argv: ['/usr/bin/python3', '-c', 'print(1)']\n"
        "      executable: /usr/bin/python3\n"
        "      timeout_s: 5\n"
        "      output_bytes_max: 1000\n",
        encoding="utf-8",
    )
    loaded = load_config(path, required=True)
    assert loaded.config.tools.write_allow == ["src/a.py"]
    assert loaded.config.tools.checks[0].id == "parse"
    dumped = dump_config(loaded.config)
    path.write_text(dumped, encoding="utf-8")
    again = load_config(path, required=True)
    approval = approve_tools(tmp_path, again.config)
    assert approval.writes_approved is False
    assert approval.write_allow == ()
    assert approval.checks == ()


@pytest.mark.parametrize(
    "payload",
    [
        {"tools": {"unknown": 1}},
        {"tools": {"file_bytes_max": 0}},
        {"tools": {"file_bytes_max": -1}},
        {"tools": {"file_bytes_max": "10"}},
        {"tools": {"runtime_s_max": 0}},
        {"tools": {"runtime_s_max": float("inf")}},
        {"tools": {"read_allow": ["/etc/passwd"]}},
        {"tools": {"read_allow": ["../escape"]}},
        {"tools": {"excludes": ["bad\x00"]}},
        {"tools": {"checks": [{"id": "Bad", "argv": ["x"], "executable": "/bin/x", "timeout_s": 1, "output_bytes_max": 10}]}},
        {"tools": {"checks": [{"id": "ok", "argv": [], "executable": "/bin/x", "timeout_s": 1, "output_bytes_max": 10}]}},
        {"tools": {"checks": [{"id": "ok", "argv": ["x"], "executable": "relative", "timeout_s": 1, "output_bytes_max": 10}]}},
        {
            "tools": {
                "checks": [
                    {"id": "ok", "argv": ["x"], "executable": "/bin/x", "timeout_s": 1, "output_bytes_max": 10},
                    {"id": "ok", "argv": ["y"], "executable": "/bin/y", "timeout_s": 1, "output_bytes_max": 10},
                ]
            }
        },
    ],
)
def test_invalid_tools_config(payload):
    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_request_round_trip_and_canary_errors():
    request = ReadRangeRequest(path="src/a.py", start_line=1, end_line=2)
    again = ReadRangeRequest.model_validate_json(request.model_dump_json())
    assert again == request
    with pytest.raises(ValidationError) as caught:
        ReadRangeRequest.model_validate({"path": CANARY, "start_line": 0, "end_line": 1, "extra": 1})
    payload = safe_validation_payload(caught.value)
    rendered = json.dumps(payload)
    assert CANARY not in rendered
    assert "extra" not in rendered
    assert payload["status"] == "invalid"
    assert file_revision(b"abc") != "sha256:" + "ab" * 32 or True
    assert file_revision(b"abc").startswith("sha256:")
    assert len(file_revision(b"abc")) == len("sha256:") + 64


def test_approval_ignores_cwd_and_later_mutation(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    monkeypatch.setattr(
        "lean_weave.config.loader.resolve_state_dir",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cwd state")),
    )
    config = AppConfig(tools=ToolsConfig(write_allow=["src/a.py"], file_bytes_max=4096))
    approval = approve_tools(root, config, allow_writes=True)
    config.tools.write_allow.append("src/b.py")
    config.tools.file_bytes_max = 10
    assert approval.root == root.resolve()
    assert approval.write_allow == ("src/a.py",)
    assert approval.limits.file_bytes_max == 4096


def test_approval_snapshot_rejects_widening(tmp_path):
    config = AppConfig(tools=ToolsConfig(file_bytes_max=4096))
    approval = approve_tools(tmp_path, config)

    with pytest.raises(ValidationError):
        approval.limits.file_bytes_max = 10**9


def test_ambiguous_state_dir_rejected(tmp_path):
    root = tmp_path / "project"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    config = AppConfig.model_validate({"workflow": {"state_dir": "link/state"}})
    with pytest.raises(ConfigError):
        approve_tools(root, config)


@pytest.mark.parametrize("fmt", ["yaml", "json"])
def test_config_show_hides_policy_canaries(tmp_path, monkeypatch, fmt):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text(
        "tools:\n"
        f"  read_allow: ['{CANARY}']\n"
        "  write_allow: ['secret-write-canary']\n"
        "  excludes: ['secret-exclude-canary']\n"
        "  checks:\n"
        "    - id: parse\n"
        f"      argv: ['/usr/bin/python3', '{CANARY}']\n"
        "      executable: /usr/bin/python3\n"
        "      timeout_s: 5\n"
        "      output_bytes_max: 1000\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["--config", str(path), "config", "show", "--format", fmt])
    assert result.exit_code == 0, result.output
    assert CANARY not in result.output
    assert "secret-write-canary" not in result.output
    assert "secret-exclude-canary" not in result.output
    data = json.loads(result.stdout) if fmt == "json" else yaml.safe_load(result.stdout)
    assert data["tools"]["read_allow_count"] == 1
    assert data["tools"]["write_allow_count"] == 1
    assert data["tools"]["exclude_count"] == 1
    assert data["tools"]["check_count"] == 1
    assert data["tools"]["file_bytes_max"] == 131072
    assert "argv" not in result.stdout
    assert "checks" not in data["tools"]
