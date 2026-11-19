import sys

from lean_weave.config.models import AppConfig, ApprovedCheck, ToolsConfig
from lean_weave.executor.approval import approve_tools
from lean_weave.verify.checks import run_checks

CANARY = "sk-" + ("e" * 24)


def _approval(root, checks, **limits):
    values = {"runtime_s_max": 2, "capture_bytes_max": 4096}
    values.update(limits)
    config = AppConfig(tools=ToolsConfig(checks=checks, **values))
    return approve_tools(root, config, allow_checks=True)


def _check(check_id, argv, **kwargs):
    return ApprovedCheck(
        id=check_id,
        argv=argv,
        executable=kwargs.get("executable", argv[0]),
        timeout_s=kwargs.get("timeout_s", 2),
        output_bytes_max=kwargs.get("output_bytes_max", 2048),
        runs_workspace_code=kwargs.get("runs_workspace_code", False),
    )


def test_preflight_starts_nothing(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    marker = tmp_path / "spawned"
    helper = tmp_path / "touch.py"
    helper.write_text("import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('x')\n", encoding="utf-8")
    good = _check("touch", [sys.executable, str(helper), str(marker)])
    approval = _approval(workspace, [good])
    unknown = run_checks(approval, {"check_ids": ["touch", "missing"]})
    assert unknown.status == "unknown_check"
    duplicate = run_checks(approval, {"check_ids": ["touch", "touch"]})
    assert duplicate.status == "duplicate_check"
    empty = run_checks(approval, {"check_ids": []})
    assert empty.status == "invalid"
    assert not marker.exists()
    blocked = _check("isolated", [sys.executable, "-c", "print(1)"], runs_workspace_code=True)
    mixed = _approval(workspace, [good, blocked])
    result = run_checks(mixed, {"check_ids": ["touch", "isolated"]})
    assert result.ok is False
    assert result.status == "blocked"
    assert [item.status for item in result.items] == ["not_run", "blocked"]
    assert not marker.exists()


def test_order_success_and_failure(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    helper = tmp_path / "exit.py"
    helper.write_text("import sys\nraise SystemExit(int(sys.argv[1]))\n", encoding="utf-8")
    approval = _approval(
        workspace,
        [
            _check("ok", [sys.executable, str(helper), "0"]),
            _check("bad", [sys.executable, str(helper), "2"]),
        ],
    )
    result = run_checks(approval, {"check_ids": ["ok", "bad"]})
    assert [item.status for item in result.items] == ["passed", "failed"]
    assert result.items[1].exit_code == 2
    assert result.ok is False


def test_diagnostics_drop_canaries(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "src").mkdir()
    (workspace / "src/a.py").write_text("x = 1\n", encoding="utf-8")
    helper = tmp_path / "diag.py"
    helper.write_text(
        "import sys\n"
        "print('src/a.py:1:1: E999 secret in the message')\n"
        "print('src/a.py:1:1: E999 secret in the message')\n"
        "print('.env:1:1: E999 hidden')\n"
        "print(sys.argv[1])\n"
        "print('not a diagnostic')\n",
        encoding="utf-8",
    )
    approval = _approval(workspace, [_check("diag", [sys.executable, str(helper), CANARY])])
    result = run_checks(approval, {"check_ids": ["diag"]})
    rendered = result.model_dump_json()
    assert CANARY not in rendered
    assert "secret in the message" not in rendered
    assert ".env" not in rendered
    codes = [item.code for item in result.items[0].diagnostics]
    located = [item for item in result.items[0].diagnostics if item.code == "E999" and item.path]
    assert len(located) == 1
    assert "UNRECOGNIZED" in codes
    assert result.items[0].diagnostic_omissions >= 1
    kept = next(item for item in result.items[0].diagnostics if item.code == "E999")
    assert kept.path == "src/a.py"
    assert kept.summary == "Python syntax error"


def test_batch_deadline_marks_unstarted(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    helper = tmp_path / "sleep.py"
    helper.write_text("import sys, time\ntime.sleep(float(sys.argv[1]))\n", encoding="utf-8")
    approval = _approval(
        workspace,
        [
            _check("fast", [sys.executable, str(helper), "0.1"], timeout_s=2),
            _check("slow", [sys.executable, str(helper), "5"], timeout_s=2),
            _check("later", [sys.executable, str(helper), "0.1"], timeout_s=2),
        ],
    )
    result = run_checks(approval)
    assert result.items[0].status == "passed"
    assert result.items[1].status == "timed_out"
    assert result.items[2].status == "not_run"
    assert result.ok is False


def test_no_checks_is_not_a_pass(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    bare = approve_tools(workspace, AppConfig())
    assert run_checks(bare).status == "no_checks"
    approved_empty = approve_tools(workspace, AppConfig(), allow_checks=True)
    assert run_checks(approved_empty).status == "no_checks"
    assert run_checks(approved_empty).ok is False


def test_output_limited_check_is_not_a_pass(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    helper = tmp_path / "big.py"
    helper.write_text("import sys\nsys.stdout.write('Z' * 100000)\n", encoding="utf-8")
    approval = _approval(
        workspace,
        [_check("big", [sys.executable, str(helper)], output_bytes_max=64)],
        capture_bytes_max=64,
    )

    result = run_checks(approval, {"check_ids": ["big"]})
    assert result.ok is False
    assert result.items[0].status == "output_limited"
    assert result.items[0].diagnostics[0].code == "OUTPUT_LIMIT"
