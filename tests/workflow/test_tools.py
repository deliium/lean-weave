import sys

import pytest

import lean_weave.workflow.tools as tools_module
from lean_weave.config.models import AppConfig, ApprovedCheck, ToolsConfig
from lean_weave.errors import ForbiddenTool
from lean_weave.executor.approval import approve_tools
from lean_weave.executor.models import ApplyPatchSuccess
from lean_weave.executor.source import read_range
from lean_weave.schemas import FeatureState
from lean_weave.verify.runner import ApprovedChecksRunner, repair_attribution
from lean_weave.workflow.tools import RegistryCodeExecutor, WorkflowTools

FILE = "src/a.py"


def _check(check_id, script, *, runs_workspace_code=False, timeout_s=2):
    return ApprovedCheck(
        id=check_id,
        argv=[sys.executable, str(script)],
        executable=sys.executable,
        timeout_s=timeout_s,
        output_bytes_max=4096,
        runs_workspace_code=runs_workspace_code,
    )


def _approval(tmp_path, checks, *, write_allow=(FILE,)):
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / FILE).write_text("value = 1\n", encoding="utf-8")
    (root / "src/b.py").write_text("other = 2\n", encoding="utf-8")
    config = AppConfig(tools=ToolsConfig(write_allow=list(write_allow), checks=checks))
    return approve_tools(root, config, allow_writes=True, allow_checks=True)


def _helper(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _diff(old, new, line=1):
    return (
        f"--- a/{FILE}\n+++ b/{FILE}\n"
        f"@@ -{line},1 +{line},1 @@\n-{old}\n+{new}\n"
    )


def test_tools_scope_stale_and_unconfirmed(tmp_path):
    approval = _approval(tmp_path, [], write_allow=(FILE, "src/b.py"))
    tools = WorkflowTools(approval, task_files=[FILE], checks_ready=True)
    outside = tools.dispatch("apply_patch", {"path": "src/b.py", "expected_revision": "sha256:" + "0" * 64, "diff": ""})
    assert outside.status == "denied"
    unknown = tools.dispatch("run_shell", {"argv": ["rm", "-rf", "/"]})
    assert unknown.status == "invalid"
    stale = tools.dispatch(
        "apply_patch",
        {"path": FILE, "expected_revision": "sha256:" + "0" * 64, "diff": _diff("value = 1", "value = 2")},
    )
    assert stale.status == "stale"
    assert stale.committed is False

    current = read_range(approval, {"path": FILE, "start_line": 1, "end_line": 1})
    applied = tools.dispatch(
        "apply_patch",
        {"path": FILE, "expected_revision": current.revision, "diff": _diff("value = 1", "value = 2")},
    )
    assert applied.status == "committed"
    assert applied.committed is True
    assert (approval.root / FILE).read_text(encoding="utf-8") == "value = 2\n"

    def unconfirmed(_approval, _request):
        return ApplyPatchSuccess(
            revision="sha256:" + "1" * 64,
            lines_added=1,
            lines_removed=1,
            durability="unconfirmed",
        )

    original = tools_module.apply_patch
    tools_module.apply_patch = unconfirmed
    try:
        result = tools.dispatch(
            "apply_patch",
            {"path": FILE, "expected_revision": current.revision, "diff": _diff("a", "b")},
        )
    finally:
        tools_module.apply_patch = original
    assert result.committed is True
    assert result.unconfirmed is True
    assert result.status == "committed_unconfirmed"


def test_checks_ready_refuses_writes(tmp_path):
    approval = _approval(tmp_path, [])
    tools = WorkflowTools(approval, task_files=[FILE], checks_ready=False)
    blocked = tools.dispatch(
        "apply_patch",
        {"path": FILE, "expected_revision": "sha256:" + "0" * 64, "diff": ""},
    )
    assert blocked.status == "blocked"


def test_runner_pass_and_repairable_failure(tmp_path):
    passed = _helper(tmp_path, "pass.py", "raise SystemExit(0)\n")
    failed = _helper(tmp_path, "fail.py", "import sys\nprint('src/a.py:1:1: E999 broken')\nraise SystemExit(1)\n")
    approval = _approval(tmp_path, [_check("pass", passed), _check("fail", failed)])
    good = ApprovedChecksRunner(approval, check_ids=["pass"]).run(FeatureState(feature_id="f"))
    assert good.status.value == "pass"
    assert good.blocking is False
    bad_runner = ApprovedChecksRunner(approval, check_ids=["fail"])
    bad = bad_runner.run(FeatureState(feature_id="f"))
    assert bad.status.value == "fail"
    assert bad.blocking is True
    assert bad.suggested_next == "retry"
    assert bad.affected_files == [FILE]
    attribution = repair_attribution(bad.blockers, {FILE}, bad.affected_files)
    assert attribution is not None
    assert attribution.paths == (FILE,)
    assert attribution.blockers[0]["code"] == "E999"


def test_unrecognized_timeout_and_missing_backend_do_not_authorize_repair(tmp_path):
    noise = _helper(tmp_path, "noise.py", "print('noise without a diagnostic')\nraise SystemExit(1)\n")
    slow = _helper(tmp_path, "slow.py", "import time\ntime.sleep(5)\n")
    isolated = _check("isolated", noise, runs_workspace_code=True)
    approval = _approval(tmp_path, [_check("noise", noise), _check("slow", slow, timeout_s=1), isolated])
    noise_result = ApprovedChecksRunner(approval, check_ids=["noise"]).run(FeatureState(feature_id="f"))
    assert noise_result.suggested_next is None
    assert repair_attribution(noise_result.blockers, {FILE}, noise_result.affected_files) is None
    slow_result = ApprovedChecksRunner(approval, check_ids=["slow"]).run(FeatureState(feature_id="f"))
    assert slow_result.suggested_next is None
    runner = ApprovedChecksRunner(approval, check_ids=["isolated"])
    assert runner.preflight() == "isolation_unavailable"
    refused = runner.run(FeatureState(feature_id="f"))
    assert refused.blockers == [{"code": "isolation_unavailable"}]


def test_preflight_missing_and_unknown_checks(tmp_path):
    approval = _approval(tmp_path, [])
    assert ApprovedChecksRunner(approval, check_ids=[]).preflight() == "no_required_checks"
    assert ApprovedChecksRunner(approval, check_ids=["missing"]).preflight() == "unknown_check"


def test_registry_executor_rejects_unapproved_commands(tmp_path):
    passed = _helper(tmp_path, "pass.py", "raise SystemExit(0)\n")
    approval = _approval(tmp_path, [_check("pass", passed)])
    executor = RegistryCodeExecutor(approval)
    with pytest.raises(ForbiddenTool):
        executor.run({"argv": ["/bin/echo", "hi"], "cwd": str(approval.root), "timeout_s": 1})
    with pytest.raises(ForbiddenTool):
        executor.run(
            {"argv": [sys.executable, str(passed)], "cwd": str(approval.root), "timeout_s": 1, "allow_network": True}
        )
    with pytest.raises(ForbiddenTool):
        executor.run(
            {"argv": [sys.executable, str(passed)], "cwd": str(tmp_path), "timeout_s": 1}
        )
    result = executor.run(
        {"argv": [sys.executable, str(passed)], "cwd": str(approval.root), "timeout_s": 1}
    )
    assert result["returncode"] == 0
    assert result["timed_out"] is False
