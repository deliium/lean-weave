import os
import sys
from pathlib import Path

import pytest

from lean_weave.config.models import AppConfig, ApprovedCheck, ToolsConfig
from lean_weave.executor.approval import approve_tools
from lean_weave.verify.process import bind_local, local_environment, run_bounded


def _check(argv, *, workspace_code=False, timeout=2, output=1024, executable=None):
    return ApprovedCheck(
        id="probe",
        argv=argv,
        executable=executable or argv[0],
        timeout_s=timeout,
        output_bytes_max=output,
        runs_workspace_code=workspace_code,
    )


def test_environment_cwd_and_canary(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    helper = tmp_path / "env.py"
    report = tmp_path / "env.txt"
    helper.write_text(
        "import os, pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('\\n'.join(f'{k}={v}' for k, v in sorted(os.environ.items())))\n"
        "pathlib.Path(sys.argv[2]).write_text(os.getcwd())\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "super-secret-value")
    monkeypatch.setenv("PATH", "/evil/bin")
    monkeypatch.setenv("PYTHONPATH", "/evil/python")
    capture = run_bounded(
        [sys.executable, str(helper), str(report), str(tmp_path / "cwd.txt")],
        cwd=workspace,
        env=local_environment(),
        timeout_s=5,
        output_bytes_max=4096,
    )
    assert capture.status == "exited"
    assert capture.exit_code == 0
    rendered = report.read_text(encoding="utf-8")
    assert "super-secret-value" not in rendered
    assert "DEEPSEEK_API_KEY" not in rendered
    assert "/evil/bin" not in rendered
    assert "PYTHONPATH" not in rendered
    assert (tmp_path / "cwd.txt").read_text(encoding="utf-8") == str(workspace)


def test_timeout_kills_descendants(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    helper = tmp_path / "sleep.py"
    parent = tmp_path / "parent.pid"
    child = tmp_path / "child.pid"
    alive = tmp_path / "alive"
    helper.write_text(
        "import os, sys, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    open(sys.argv[2], 'w').write(str(os.getpid()))\n"
        "    time.sleep(30)\n"
        "    open(sys.argv[3], 'w').write('alive')\n"
        "    os._exit(0)\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    capture = run_bounded(
        [sys.executable, str(helper), str(parent), str(child), str(alive)],
        cwd=workspace,
        env=local_environment(),
        timeout_s=0.8,
        output_bytes_max=1024,
    )
    assert capture.status == "timed_out"
    assert not alive.exists()
    if child.exists():
        with pytest.raises(ProcessLookupError):
            os.kill(int(child.read_text()), 0)


def test_output_limit_and_bind_rejects_workspace_executable(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    helper = tmp_path / "big.py"
    helper.write_text("import sys\nsys.stdout.write('Z'*100000)\n", encoding="utf-8")
    capture = run_bounded(
        [sys.executable, str(helper)],
        cwd=workspace,
        env=local_environment(),
        timeout_s=5,
        output_bytes_max=128,
    )
    assert capture.status == "output_limited"
    inside = workspace / "tool.py"
    inside.write_text("print('no')\n", encoding="utf-8")
    approval_root = workspace
    assert bind_local(approval_root, _check([str(inside)])) is None
    marker = tmp_path / "spawned"
    bound = bind_local(workspace, _check([sys.executable, str(helper)]))
    assert bound is not None
    assert bound[0] == str(Path(sys.executable).resolve())
    assert not marker.exists()


def test_spawn_error_does_not_raise(tmp_path):
    capture = run_bounded(
        ["/missing/lean-weave-tool"],
        cwd=tmp_path,
        env=local_environment(),
        timeout_s=1,
        output_bytes_max=100,
    )
    assert capture.status == "spawn_error"
    _ = AppConfig(tools=ToolsConfig())
    _ = approve_tools
