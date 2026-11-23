"""Offline tests for FeatureSession composition."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lean_weave.errors import ConfigError
from lean_weave.providers.mock import MockLLMProvider
from lean_weave.schemas import SCHEMA_VERSION, TokenUsage, VerificationResult, VerificationStatus
from lean_weave.workflow.session import FeatureSession


def _run(root: Path, *args: str) -> None:
    subprocess.run(
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
    (root / "src" / "a.py").write_text("value = 1\n", encoding="utf-8")
    _run(root, "add", "src/a.py")
    _run(root, "commit", "-qm", "base")
    return root


def _operator_config(
    tmp_path: Path, parser: Path, *, state_dir: str | None = None
) -> Path:
    path = tmp_path / "operator.yaml"
    lines = [
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
    if state_dir is not None:
        lines += ["workflow:", f"  state_dir: {state_dir}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _parser(tmp_path: Path) -> Path:
    path = tmp_path / "parse.sh"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _plan_response():
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


def _final():
    return {
        "message": {"role": "assistant", "content": "done"},
        "usage": TokenUsage(prompt_tokens=4, completion_tokens=6, total_tokens=10),
        "usage_reported": True,
    }


def test_session_auto_preserves_origin_and_uses_worktree(tmp_path: Path):
    origin = _repo(tmp_path)
    before = (origin / "src" / "a.py").read_bytes()
    head = subprocess.run(
        ["git", "-C", str(origin), "rev-parse", "HEAD"],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    parser = _parser(tmp_path)
    config = _operator_config(tmp_path, parser)
    provider = MockLLMProvider([_plan_response(), _final()])
    session = FeatureSession(source_cwd=origin, llm_provider=provider)
    view = session.start("edit a", mode="auto", config_path=config)
    assert view.status == "success"
    assert view.worktree_path is not None
    assert Path(view.worktree_path).resolve() != origin.resolve()
    assert (origin / "src" / "a.py").read_bytes() == before
    assert (
        subprocess.run(
            ["git", "-C", str(origin), "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
        == head
    )
    status = session.status(view.feature_id)
    assert status.status == "success"
    result = session.result(view.feature_id)
    assert result.workflow is not None
    assert result.workflow.status.value == "success"


def test_operator_config_inside_checkout_refused(tmp_path: Path):
    origin = _repo(tmp_path)
    bad = origin / "operator.yaml"
    bad.write_text("version: 1\n", encoding="utf-8")
    session = FeatureSession(source_cwd=origin, llm_provider=MockLLMProvider([]))
    with pytest.raises(ConfigError, match="operator_config_inside_checkout"):
        session.start("edit a", mode="auto", config_path=bad)


def test_review_pause_and_approval(tmp_path: Path):
    origin = _repo(tmp_path)
    parser = _parser(tmp_path)
    config = _operator_config(tmp_path, parser)
    provider = MockLLMProvider([_plan_response(), _final()])
    session = FeatureSession(source_cwd=origin, llm_provider=provider)
    paused = session.start("edit a", mode="review", config_path=config)
    assert paused.status == "interrupted"
    assert paused.reason == "review_pending"
    assert paused.approval_token
    assert len(provider.requests) == 1
    done = session.resume(
        paused.feature_id,
        config_path=config,
        approve_token=paused.approval_token,
    )
    assert done.status == "success"
    assert [item["stage"] for item in provider.requests] == ["plan", "implement"]


def test_review_rejects_file_edits_made_after_pause(tmp_path: Path):
    origin = _repo(tmp_path)
    parser = _parser(tmp_path)
    config = _operator_config(tmp_path, parser)
    provider = MockLLMProvider([_plan_response()])
    session = FeatureSession(source_cwd=origin, llm_provider=provider)
    paused = session.start("edit a", mode="review", config_path=config)
    assert paused.approval_token
    worktree = Path(paused.worktree_path)
    (worktree / "src" / "a.py").write_text("TAMPERED\n", encoding="utf-8")

    stale = session.resume(
        paused.feature_id,
        config_path=config,
        approve_token=paused.approval_token,
    )
    assert stale.status == "interrupted"
    assert stale.reason == "stale_baseline"
    assert [request["stage"] for request in provider.requests] == ["plan"]
    assert (worktree / "src" / "a.py").read_text(encoding="utf-8") == "TAMPERED\n"


def test_custom_state_dir_is_frozen_and_terminal_resume_works(tmp_path: Path):
    origin = _repo(tmp_path)
    parser = _parser(tmp_path)
    config = _operator_config(tmp_path, parser, state_dir=".state-x")
    provider = MockLLMProvider([_plan_response(), _final()])
    session = FeatureSession(source_cwd=origin, llm_provider=provider)
    view = session.start("edit a", mode="auto", config_path=config)
    assert view.status == "success"
    record = session._manager.lookup(view.feature_id)
    assert record.state_dir == ".state-x"
    assert (Path(record.worktree_root) / ".state-x" / "workflow").is_dir()
    terminal = session.resume(view.feature_id)
    assert terminal.status == "success"


def test_cleanup_keeps_terminal_result_readable_from_archive(tmp_path: Path):
    origin = _repo(tmp_path)
    parser = _parser(tmp_path)
    config = _operator_config(tmp_path, parser)
    provider = MockLLMProvider([_plan_response(), _final()])
    session = FeatureSession(source_cwd=origin, llm_provider=provider)
    view = session.start("edit a", mode="auto", config_path=config)
    assert view.status == "success"

    cleaned = session.cleanup_worktree(view.feature_id, confirm=True)
    assert cleaned["phase"] == "tombstone"
    archived = session.result(view.feature_id)
    assert archived.archived is True
    assert archived.status == "success"
    assert archived.workflow is not None
    assert archived.workflow.status.value == "success"
    assert archived.change_state == "archived"


def test_durable_cancel_wins_over_resume(tmp_path: Path):
    origin = _repo(tmp_path)
    parser = _parser(tmp_path)
    config = _operator_config(tmp_path, parser)
    provider = MockLLMProvider([_plan_response()])
    session = FeatureSession(source_cwd=origin, llm_provider=provider)
    paused = session.start("edit a", mode="review", config_path=config)
    assert paused.approval_token

    with session._manager.lease(paused.feature_id):
        pending = session.cancel(paused.feature_id)
        assert pending.cancel_requested is True

    cancelled = session.resume(
        paused.feature_id,
        config_path=config,
        approve_token=paused.approval_token,
    )
    assert cancelled.status == "cancelled"
    assert cancelled.cancel_requested is False
    assert [request["stage"] for request in provider.requests] == ["plan"]


def test_v2_authority_digest_is_versioned_and_v1_stays_legacy(tmp_path: Path):
    from lean_weave.app import AppContext
    from lean_weave.config import AppConfig, load_config
    from lean_weave.workflow.lifecycle import authority_digest
    from lean_weave.workflow.session import FeatureSession

    root = _repo(tmp_path)
    parser = _parser(tmp_path)
    config_path = _operator_config(tmp_path, parser)
    session = FeatureSession(source_cwd=root)

    v1 = load_config(config_path, required=True).config
    approval = session._build_approval(root, v1, config_path)
    v1_digest = session._authority_digest(v1, approval, ("parse",))
    legacy = authority_digest(
        write_allow=approval.write_allow,
        checks=[
            {
                "id": check.id,
                "executable": check.executable,
                "argv": list(check.argv),
                "timeout_s": check.timeout_s,
                "output_bytes_max": check.output_bytes_max,
                "runs_workspace_code": check.runs_workspace_code,
            }
            for check in approval.checks
        ],
        check_ids=("parse",),
        state_dir=v1.workflow.state_dir,
        max_retries=v1.workflow.max_retries,
        max_task_tokens=v1.workflow.max_task_tokens,
        feature_token_budget=v1.workflow.feature_token_budget,
        max_task_llm_calls=v1.workflow.max_task_llm_calls,
    )
    assert v1_digest == legacy

    v2 = v1.model_copy(
        update={"context": v1.context.model_copy(update={"workflow_mode": "v2"})}
    )
    v2_digest = session._authority_digest(v2, approval, ("parse",))
    assert v2_digest != v1_digest
    changed = v2.model_copy(
        update={
            "context": v2.context.model_copy(
                update={
                    "packing": v2.context.packing.model_copy(
                        update={"source_tokens": 2048}
                    )
                }
            )
        }
    )
    assert session._authority_digest(changed, approval, ("parse",)) != v2_digest

    # Disabled dependency coverage must keep the prior digest shape (key absent).
    from lean_weave.config.models import DependencyCoverageConfig

    still_disabled = v2.model_copy(
        update={
            "context": v2.context.model_copy(
                update={"dependency_coverage": DependencyCoverageConfig(enabled=False)}
            )
        }
    )
    assert session._authority_digest(still_disabled, approval, ("parse",)) == v2_digest

    enabled = v2.model_copy(
        update={
            "context": v2.context.model_copy(
                update={
                    "dependency_coverage": DependencyCoverageConfig(
                        enabled=True, max_depth=1, max_tokens=256
                    )
                }
            )
        }
    )
    enabled_digest = session._authority_digest(enabled, approval, ("parse",))
    assert enabled_digest != v2_digest
    tighter = enabled.model_copy(
        update={
            "context": enabled.context.model_copy(
                update={
                    "dependency_coverage": DependencyCoverageConfig(
                        enabled=True, max_depth=1, max_tokens=128
                    )
                }
            )
        }
    )
    assert session._authority_digest(tighter, approval, ("parse",)) != enabled_digest
