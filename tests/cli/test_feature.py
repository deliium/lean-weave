"""CLI wiring tests for feature workflow commands."""

from __future__ import annotations

from typer.testing import CliRunner

from lean_weave.cli.app import app

runner = CliRunner()


def test_bare_feature_shows_help_without_config():
    result = runner.invoke(app, ["feature"])
    assert result.exit_code == 0
    assert "auto" in result.stdout.lower() or "Feature" in result.stdout or "feature" in result.stdout


def test_feature_requires_mode_flag():
    result = runner.invoke(app, ["feature", "do something"])
    assert result.exit_code != 0


def test_status_help_and_aliases_registered():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("feature", "status", "resume", "cancel", "result", "worktree"):
        assert name in result.stdout


def _view(*, token=None, command=None, archived=False):
    from lean_weave.schemas import TokenCounters, VerificationResult, VerificationStatus
    from lean_weave.workflow.results import TaskResult, WorkflowResult, WorkflowStatus
    from lean_weave.workflow.session import FeatureView

    workflow = WorkflowResult(
        feature_id="abc123",
        status=WorkflowStatus.SUCCESS,
        reason="verification_passed",
        verification=VerificationResult(
            status=VerificationStatus.PASS,
            gate_summaries=["parse:passed"],
        ),
        tasks=[TaskResult(id="1", title="edit", calls=1, tokens=10)],
        counters=TokenCounters(prompt_tokens=4, completion_tokens=6, llm_calls=2),
    )
    return FeatureView(
        feature_id="abc123",
        status="success",
        reason="verification_passed",
        stage="done",
        worktree_path="/tmp/wt",
        branch="feature/abc123",
        next_action="result",
        plan_digest="sha256:" + "a" * 64,
        approval_token=token,
        approval_command=command,
        modified_paths=("src/a.py",),
        workflow=workflow,
        plan_goal="edit one file",
        plan_tasks=(("1", "edit", ("src/a.py",)),),
        change_state="modified",
        change_summary="staged:0,unstaged:1,untracked:0",
        archived=archived,
        archive_path="/tmp/archive" if archived else None,
    )


def test_result_renders_plan_changes_verification_and_usage(monkeypatch):
    monkeypatch.setattr(
        "lean_weave.cli.commands.feature.FeatureSession.status",
        lambda self, feature_id: _view(),
    )
    result = runner.invoke(app, ["status", "abc123"])
    assert result.exit_code == 0
    assert "changes=staged:0,unstaged:1,untracked:0" in result.stdout
    assert "plan_goal=edit one file" in result.stdout
    assert "plan_task=1: edit [src/a.py]" in result.stdout
    assert "verification=pass blockers=0" in result.stdout
    assert "usage=prompt:4,completion:6,reasoning:0,total:10" in result.stdout
    assert "modified=1" in result.stdout


def test_review_view_prints_exact_approval_command(monkeypatch):
    view = _view(
        token="deadbeef",
        command="wl --config /secure/operator.yaml resume abc123 --approve deadbeef",
    )
    monkeypatch.setattr(
        "lean_weave.cli.commands.feature.FeatureSession.status",
        lambda self, feature_id: view,
    )
    result = runner.invoke(app, ["status", "abc123"])
    assert result.exit_code == 0
    assert "approve=wl --config /secure/operator.yaml resume abc123 --approve deadbeef" in result.stdout


def test_terminal_resume_does_not_require_config(monkeypatch):
    calls = {}

    def fake_resume(self, feature_id, *, config_path=None, approve_token=None):
        calls["config"] = config_path
        return _view()

    monkeypatch.setattr(
        "lean_weave.cli.commands.feature.FeatureSession.status",
        lambda self, feature_id: _view(),
    )
    monkeypatch.setattr(
        "lean_weave.cli.commands.feature.FeatureSession.resume",
        fake_resume,
    )
    result = runner.invoke(app, ["resume", "abc123"])
    assert result.exit_code == 0
    assert calls["config"] is None


def test_cleanup_requires_explicit_confirmation():
    result = runner.invoke(app, ["worktree", "cleanup", "abc123"])
    assert result.exit_code != 0
    assert "cleanup_unconfirmed" in result.stdout or "cleanup_unconfirmed" in result.stderr
