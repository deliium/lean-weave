"""Offline tests for the fixed worktree Git adapter."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from lean_weave.errors import GitOperationError, UnsupportedRepository
from lean_weave.worktree import git as git_module
from lean_weave.worktree.git import (
    add_worktree,
    list_worktrees,
    preflight_repository,
    read_status,
    remove_worktree,
    resolve_git_executable,
)


def _run(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "PATH": "/usr/bin:/bin",
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
            "HOME": str(root),
        },
    )


def _init_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _run(root, "init", "-q")
    _run(root, "config", "user.email", "fixture@example.invalid")
    _run(root, "config", "user.name", "Fixture")
    (root / "README").write_text("base\n", encoding="utf-8")
    _run(root, "add", "README")
    _run(root, "commit", "-qm", "base")
    return root


def test_resolve_git_executable_is_absolute():
    path = resolve_git_executable()
    assert path.is_absolute()
    assert path.name == "git"
    assert path.is_file()


def test_preflight_primary_and_linked_worktree(tmp_path: Path):
    root = _init_repo(tmp_path / "src")
    identity = preflight_repository(root)
    assert identity.root == root.resolve()
    assert identity.common_dir == (root / ".git").resolve()
    assert identity.git_dir == identity.common_dir
    assert len(identity.head) == 40
    assert identity.branch is not None

    linked = add_worktree(
        identity,
        branch="feature/demo",
        path=tmp_path / "wt-demo",
        start=identity.head,
    )
    assert linked.root == (tmp_path / "wt-demo").resolve()
    assert linked.common_dir == identity.common_dir
    assert linked.git_dir != linked.common_dir
    assert (linked.root / ".git").is_file()
    assert linked.branch == "feature/demo"
    assert linked.head == identity.head

    entries = list_worktrees(identity)
    paths = {entry.path for entry in entries}
    assert identity.root in paths
    assert linked.root in paths

    remove_worktree(identity, linked.root)
    assert not linked.root.exists()
    remaining = {entry.path for entry in list_worktrees(identity)}
    assert linked.root not in remaining
    assert identity.root in remaining


def test_unsupported_filter_config_does_not_execute(tmp_path: Path):
    root = _init_repo(tmp_path / "src")
    canary = tmp_path / "canary"
    _run(root, "config", "filter.evil.clean", f"sh -c 'touch {canary}'")
    with pytest.raises(UnsupportedRepository, match="unsafe_config"):
        preflight_repository(root)
    assert not canary.exists()


def test_unsupported_hooks_path_and_executable_hook(tmp_path: Path):
    root = _init_repo(tmp_path / "src")
    hooks = tmp_path / "custom-hooks"
    hooks.mkdir()
    _run(root, "config", "core.hooksPath", str(hooks))
    with pytest.raises(UnsupportedRepository, match="unsafe_config"):
        preflight_repository(root)

    _run(root, "config", "--unset", "core.hooksPath")
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    with pytest.raises(UnsupportedRepository, match="hook_executable"):
        preflight_repository(root)


def test_unsupported_include_sparse_submodule_partial(tmp_path: Path):
    root = _init_repo(tmp_path / "src")
    include = tmp_path / "extra.gitconfig"
    include.write_text("[user]\n\tname = Extra\n", encoding="utf-8")
    _run(root, "config", "include.path", str(include))
    with pytest.raises(UnsupportedRepository, match="config_include"):
        preflight_repository(root)
    _run(root, "config", "--unset", "include.path")

    _run(root, "config", "core.sparseCheckout", "true")
    with pytest.raises(UnsupportedRepository, match="sparse_checkout"):
        preflight_repository(root)
    _run(root, "config", "core.sparseCheckout", "false")

    (root / ".gitmodules").write_text("[submodule \"x\"]\n\tpath = x\n", encoding="utf-8")
    with pytest.raises(UnsupportedRepository, match="submodules"):
        preflight_repository(root)
    (root / ".gitmodules").unlink()

    _run(root, "config", "remote.origin.promisor", "true")
    with pytest.raises(UnsupportedRepository, match="partial_clone"):
        preflight_repository(root)


def test_missing_head_refused(tmp_path: Path):
    root = tmp_path / "empty"
    root.mkdir()
    _run(root, "init", "-q")
    _run(root, "config", "user.email", "fixture@example.invalid")
    _run(root, "config", "user.name", "Fixture")
    with pytest.raises(UnsupportedRepository, match="missing_head"):
        preflight_repository(root)


def test_hostile_env_git_vars_do_not_leak(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _init_repo(tmp_path / "src")
    monkeypatch.setenv("GIT_DIR", "/tmp/hostile-git-dir")
    monkeypatch.setenv("GIT_WORK_TREE", "/tmp/hostile-work-tree")
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/hostile-objects")
    monkeypatch.setenv("GIT_TRACE", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "filter.evil.clean")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")

    captured_envs: list[dict[str, str]] = []
    real = git_module.run_bounded

    def spy(argv, *, cwd, env, timeout_s, output_bytes_max):
        captured_envs.append(dict(env))
        return real(argv, cwd=cwd, env=env, timeout_s=timeout_s, output_bytes_max=output_bytes_max)

    monkeypatch.setattr(git_module, "run_bounded", spy)
    identity = preflight_repository(root)
    assert identity.root == root.resolve()
    assert captured_envs
    for env in captured_envs:
        assert "GIT_DIR" not in env
        assert "GIT_WORK_TREE" not in env
        assert "GIT_OBJECT_DIRECTORY" not in env
        assert "GIT_TRACE" not in env
        assert "GIT_CONFIG_COUNT" not in env
        assert "GIT_CONFIG_KEY_0" not in env
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_OPTIONAL_LOCKS"] == "0"
        assert env["GIT_NO_LAZY_FETCH"] == "1"
        allowed_git = {
            "GIT_CONFIG_NOSYSTEM",
            "GIT_CONFIG_GLOBAL",
            "GIT_ATTR_NOSYSTEM",
            "GIT_OPTIONAL_LOCKS",
            "GIT_TERMINAL_PROMPT",
            "GIT_NO_LAZY_FETCH",
            "GIT_ASKPASS",
            "GIT_PAGER",
        }
        leaked = [key for key in env if key.startswith("GIT_") and key not in allowed_git]
        assert leaked == []


def test_status_include_ignored_and_no_index_refresh(tmp_path: Path):
    root = _init_repo(tmp_path / "src")
    (root / ".gitignore").write_text("secret.bin\n", encoding="utf-8")
    _run(root, "add", ".gitignore")
    _run(root, "commit", "-qm", "ignore")
    (root / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    _run(root, "add", "tracked.py")
    _run(root, "commit", "-qm", "track")
    (root / "tracked.py").write_text("x = 2\n", encoding="utf-8")
    (root / "fresh.py").write_text("new\n", encoding="utf-8")
    (root / "secret.bin").write_bytes(b"\x00secret\x00")

    identity = preflight_repository(root)
    index_before = (root / ".git" / "index").read_bytes()
    content_before = {
        "tracked.py": (root / "tracked.py").read_bytes(),
        "fresh.py": (root / "fresh.py").read_bytes(),
        "secret.bin": (root / "secret.bin").read_bytes(),
    }

    plain = read_status(identity)
    paths = {entry.path: entry.xy for entry in plain.entries}
    assert "tracked.py" in paths
    assert "fresh.py" in paths
    assert "secret.bin" not in paths

    with_ignored = read_status(identity, include_ignored=True)
    ignored_paths = {entry.path: entry.xy for entry in with_ignored.entries}
    assert "secret.bin" in ignored_paths
    assert ignored_paths["secret.bin"].startswith("!")

    assert (root / ".git" / "index").read_bytes() == index_before
    for name, payload in content_before.items():
        assert (root / name).read_bytes() == payload


def test_truncated_status_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _init_repo(tmp_path / "src")
    (root / "big.py").write_text("x = 1\n", encoding="utf-8")
    identity = preflight_repository(root)

    real = git_module.run_bounded

    def limited(argv, *, cwd, env, timeout_s, output_bytes_max):
        if "status" in argv:
            return git_module.ProcessCapture("output_limited", None, b" M big.py\0", b"", 1)
        return real(argv, cwd=cwd, env=env, timeout_s=timeout_s, output_bytes_max=output_bytes_max)

    monkeypatch.setattr(git_module, "run_bounded", limited)
    with pytest.raises(GitOperationError, match="status_truncated"):
        read_status(identity)


def test_truncated_worktree_list_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _init_repo(tmp_path / "src")
    identity = preflight_repository(root)
    real = git_module.run_bounded

    def limited(argv, *, cwd, env, timeout_s, output_bytes_max):
        if "worktree" in argv and "list" in argv:
            return git_module.ProcessCapture("output_limited", None, b"worktree ", b"", 1)
        return real(argv, cwd=cwd, env=env, timeout_s=timeout_s, output_bytes_max=output_bytes_max)

    monkeypatch.setattr(git_module, "run_bounded", limited)
    with pytest.raises(GitOperationError, match="list_worktrees_truncated"):
        list_worktrees(identity)


def test_remove_worktree_never_passes_force(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _init_repo(tmp_path / "src")
    identity = preflight_repository(root)
    linked = add_worktree(
        identity,
        branch="feature/rm",
        path=tmp_path / "wt-rm",
        start=identity.head,
    )
    seen: list[list[str]] = []
    real = git_module.run_bounded

    def spy(argv, *, cwd, env, timeout_s, output_bytes_max):
        seen.append(list(argv))
        return real(argv, cwd=cwd, env=env, timeout_s=timeout_s, output_bytes_max=output_bytes_max)

    monkeypatch.setattr(git_module, "run_bounded", spy)
    remove_worktree(identity, linked.root)
    remove_calls = [argv for argv in seen if "worktree" in argv and "remove" in argv]
    assert remove_calls
    for argv in remove_calls:
        assert "--force" not in argv
        assert "-f" not in argv


def test_argv_uses_trusted_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _init_repo(tmp_path / "src")
    seen: list[list[str]] = []
    real = git_module.run_bounded

    def spy(argv, *, cwd, env, timeout_s, output_bytes_max):
        seen.append(list(argv))
        return real(argv, cwd=cwd, env=env, timeout_s=timeout_s, output_bytes_max=output_bytes_max)

    monkeypatch.setattr(git_module, "run_bounded", spy)
    preflight_repository(root)
    assert seen
    for argv in seen:
        assert Path(argv[0]).is_absolute()
        assert "core.hooksPath=" in argv
        assert "core.fsmonitor=false" in argv
