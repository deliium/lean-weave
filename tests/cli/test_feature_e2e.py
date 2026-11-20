"""Offline CLI acceptance around the public feature workflow (no network)."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lean_weave.cli.app import app
from lean_weave.errors import GitOperationError, ProviderError
from lean_weave.providers.mock import MockLLMProvider
from lean_weave.schemas import SCHEMA_VERSION, TokenUsage
from lean_weave.workflow.session import FeatureSession, FeatureView

runner = CliRunner()


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "PATH": "/usr/bin:/bin",
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "HOME": str(root),
        },
    )


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "origin"
    root.mkdir()
    _run(root, "init", "-q")
    _run(root, "config", "user.email", "fixture@example.invalid")
    _run(root, "config", "user.name", "Fixture")
    (root / "src").mkdir()
    (root / "src" / "a.py").write_bytes(b"value = 1\n")
    _run(root, "add", "src/a.py")
    _run(root, "commit", "-qm", "base")
    return root


def _parser(tmp_path: Path) -> Path:
    path = tmp_path / "parse.sh"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _config(tmp_path: Path) -> Path:
    parser = _parser(tmp_path)
    path = tmp_path / "operator.yaml"
    path.write_text(
        "\n".join(
            [
                "version: 1",
                "tools:",
                "  write_allow:",
                "    - src/a.py",
                "  checks:",
                "    - id: parse",
                f"      executable: {parser}",
                "      argv:",
                f"        - {parser}",
                "      timeout_s: 5",
                "      output_bytes_max: 65536",
                "      runs_workspace_code: false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _plan() -> dict:
    return {
        "message": {"role": "assistant", "content": None},
        "structured": {
            "version": SCHEMA_VERSION,
            "goal": "edit one file",
            "settings": {"testing": True, "logging": "verbose", "docs": True},
            "reconciliation": [],
            "tasks": [
                {
                    "id": "1",
                    "title": "edit",
                    "description": "change a",
                    "files": ["src/a.py"],
                    "depends_on": [],
                }
            ],
            "commit_checkpoints": [],
        },
        "usage": TokenUsage(prompt_tokens=4, completion_tokens=6, total_tokens=10),
        "usage_reported": True,
    }


def _final() -> dict:
    return {
        "message": {"role": "assistant", "content": "done"},
        "usage": TokenUsage(prompt_tokens=4, completion_tokens=6, total_tokens=10),
        "usage_reported": True,
    }


def _inject(monkeypatch: pytest.MonkeyPatch, origin: Path, provider: MockLLMProvider) -> None:
    def factory(*args, **kwargs):
        kwargs.setdefault("source_cwd", origin)
        kwargs.setdefault("llm_provider", provider)
        return FeatureSession(*args, **kwargs)

    monkeypatch.setattr("lean_weave.cli.commands.feature.FeatureSession", factory)


def _snapshot(origin: Path) -> dict[str, object]:
    return {
        "bytes": (origin / "src" / "a.py").read_bytes(),
        "head": _run(origin, "rev-parse", "HEAD").stdout,
        "index": (origin / ".git" / "index").read_bytes(),
        "status": _run(origin, "status", "--porcelain=v1", "-uall").stdout,
    }


def _invoke(*args: str):
    return runner.invoke(app, list(args))


def _token(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("approve=") and "--approve " in line:
            return line.split("--approve ", 1)[1].strip()
    raise AssertionError("approval token missing from output")


def test_auto_mode_uses_worktree_and_preserves_origin(tmp_path: Path, monkeypatch):
    origin = _repo(tmp_path)
    config = _config(tmp_path)
    before = _snapshot(origin)
    provider = MockLLMProvider([_plan(), _final()])
    _inject(monkeypatch, origin, provider)

    result = _invoke("--config", str(config), "feature", "edit a", "--auto")
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "status=success" in result.stdout
    assert "worktree=" in result.stdout
    assert "changes=" in result.stdout
    assert "verification=pass" in result.stdout
    assert _snapshot(origin) == before


def test_review_pause_plan_and_explicit_approval(tmp_path: Path, monkeypatch):
    origin = _repo(tmp_path)
    config = _config(tmp_path)
    provider = MockLLMProvider([_plan(), _final()])
    _inject(monkeypatch, origin, provider)

    paused = _invoke("--config", str(config), "feature", "edit a", "--review")
    assert paused.exit_code == 0, paused.stdout + paused.stderr
    assert "reason=review_pending" in paused.stdout
    assert "plan_goal=edit one file" in paused.stdout
    assert "plan_task=1: edit [src/a.py]" in paused.stdout
    token = _token(paused.stdout)
    assert _run(origin, "status", "--porcelain=v1", "-uall").stdout == b""

    done = _invoke(
        "--config",
        str(config),
        "resume",
        paused.stdout.split("feature_id=", 1)[1].splitlines()[0],
        "--approve",
        token,
    )
    assert done.exit_code == 0, done.stdout + done.stderr
    assert "status=success" in done.stdout
    assert [request["stage"] for request in provider.requests] == ["plan", "implement"]


def test_review_stale_approval_after_file_edit_is_rejected(tmp_path: Path, monkeypatch):
    origin = _repo(tmp_path)
    config = _config(tmp_path)
    provider = MockLLMProvider([_plan()])
    _inject(monkeypatch, origin, provider)

    paused = _invoke("--config", str(config), "feature", "edit a", "--review")
    feature_id = paused.stdout.split("feature_id=", 1)[1].splitlines()[0]
    token = _token(paused.stdout)
    worktree = Path(
        next(
            line.split("worktree=", 1)[1]
            for line in paused.stdout.splitlines()
            if line.startswith("worktree=")
        )
    )
    (worktree / "src" / "a.py").write_text("TAMPERED\n", encoding="utf-8")

    stale = _invoke(
        "--config", str(config), "resume", feature_id, "--approve", token
    )
    assert stale.exit_code == 0, stale.stdout + stale.stderr
    assert "reason=stale_baseline" in stale.stdout
    assert [request["stage"] for request in provider.requests] == ["plan"]
    assert (worktree / "src" / "a.py").read_text(encoding="utf-8") == "TAMPERED\n"


def test_cancel_idle_and_terminal_retrieval_without_config(tmp_path: Path, monkeypatch):
    origin = _repo(tmp_path)
    config = _config(tmp_path)
    provider = MockLLMProvider([_plan()])
    _inject(monkeypatch, origin, provider)

    paused = _invoke("--config", str(config), "feature", "edit a", "--review")
    feature_id = paused.stdout.split("feature_id=", 1)[1].splitlines()[0]
    config.unlink()
    cancelled = _invoke("cancel", feature_id)
    assert cancelled.exit_code == 0, cancelled.stdout + cancelled.stderr
    assert "status=cancelled" in cancelled.stdout

    terminal = _invoke("resume", feature_id)
    assert terminal.exit_code == 0, terminal.stdout + terminal.stderr
    assert "status=cancelled" in terminal.stdout


def test_resume_after_provider_interruption(tmp_path: Path, monkeypatch):
    origin = _repo(tmp_path)
    config = _config(tmp_path)
    provider = MockLLMProvider([_plan(), ProviderError("transport failed"), _final()])
    _inject(monkeypatch, origin, provider)

    started = _invoke("--config", str(config), "feature", "edit a", "--auto")
    assert started.exit_code == 0, started.stdout + started.stderr
    assert "reason=provider_error" in started.stdout
    feature_id = started.stdout.split("feature_id=", 1)[1].splitlines()[0]

    resumed = _invoke("--config", str(config), "resume", feature_id)
    assert resumed.exit_code == 0, resumed.stdout + resumed.stderr
    assert "status=success" in resumed.stdout
    assert [request["stage"] for request in provider.requests].count("plan") == 1


def test_duplicate_starts_and_git_failure_keep_origin_intact(tmp_path: Path, monkeypatch):
    origin = _repo(tmp_path)
    config = _config(tmp_path)
    before = _snapshot(origin)

    first = MockLLMProvider([_plan(), _final()])
    _inject(monkeypatch, origin, first)
    one = _invoke("--config", str(config), "feature", "edit a", "--auto")
    two_provider = MockLLMProvider([_plan(), _final()])
    _inject(monkeypatch, origin, two_provider)
    two = _invoke("--config", str(config), "feature", "edit a", "--auto")
    id_one = one.stdout.split("feature_id=", 1)[1].splitlines()[0]
    id_two = two.stdout.split("feature_id=", 1)[1].splitlines()[0]
    assert id_one != id_two
    assert _snapshot(origin) == before

    def boom(*_args, **_kwargs):
        raise GitOperationError("add_failed")

    monkeypatch.setattr("lean_weave.worktree.manager.add_worktree", boom)
    failed = _invoke("--config", str(config), "feature", "edit a", "--auto")
    assert failed.exit_code != 0
    assert "provision_failed" in failed.stdout + failed.stderr
    assert _snapshot(origin) == before


def test_both_entrypoints_and_no_langgraph_runtime_dependency():
    import lean_weave
    import lean_weave.cli as cli_pkg

    assert callable(lean_weave.main)
    assert callable(cli_pkg.main)
    help_text = runner.invoke(app, ["--help"]).stdout
    for name in ("feature", "status", "resume", "cancel", "result", "worktree"):
        assert name in help_text

    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    )
    dependencies = pyproject["project"]["dependencies"]
    assert not any("langgraph" in item.lower() for item in dependencies)


def test_feature_help_and_status_sentinels_do_not_need_provider(monkeypatch):
    calls = {"start": 0}

    def boom(*_args, **_kwargs):
        calls["start"] += 1
        raise AssertionError("start should not run for help")

    monkeypatch.setattr("lean_weave.cli.commands.feature.FeatureSession.start", boom)
    help_result = runner.invoke(app, ["feature"])
    assert help_result.exit_code == 0
    assert calls["start"] == 0

    def fake_status(self, feature_id: str):
        return FeatureView(
            feature_id=feature_id,
            status="success",
            reason="completed",
            stage="done",
            worktree_path="/tmp/wt",
            branch="feature/x",
            next_action="result",
        )

    monkeypatch.setattr(
        "lean_weave.cli.commands.feature.FeatureSession.status",
        fake_status,
    )
    status = runner.invoke(app, ["status", "abc123"])
    assert status.exit_code == 0
    assert "feature_id=abc123" in status.stdout
    assert "status=success" in status.stdout
