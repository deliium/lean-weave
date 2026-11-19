import subprocess
from pathlib import Path

from lean_weave.executor import inspect_git_diff
from lean_weave.executor import git_diff as git_diff_module
from tests.executor.conftest import make_approval


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init")
    git(root, "config", "user.email", "dev@example.com")
    git(root, "config", "user.name", "Dev")


def test_staged_unstaged_deleted_and_whitespace(tmp_path):
    root = tmp_path / "ws"
    init_repo(root)
    (root / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    (root / "my file.py").write_text("value = 1\n", encoding="utf-8")
    git(root, "add", "tracked.py", "my file.py")
    git(root, "commit", "-m", "base")
    (root / "gone.py").write_text("value = 1\n", encoding="utf-8")
    git(root, "add", "gone.py")
    git(root, "commit", "-m", "gone")
    (root / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    git(root, "add", "tracked.py")
    (root / "tracked.py").write_text("value = 3\n", encoding="utf-8")
    (root / "my file.py").write_text("value = 9\n", encoding="utf-8")
    (root / "gone.py").unlink()

    approval = make_approval(root)
    staged = inspect_git_diff(approval, {"mode": "staged"})
    unstaged = inspect_git_diff(approval, {"mode": "unstaged"})
    assert staged.status == "changes"
    assert {item.path for item in staged.entries} == {"tracked.py"}
    assert {item.path: item.status for item in unstaged.entries}["tracked.py"] == "M"
    assert {item.path: item.status for item in unstaged.entries}["my file.py"] == "M"
    assert {item.path: item.status for item in unstaged.entries}["gone.py"] == "D"
    before = (root / ".git" / "index").read_bytes()
    files = {
        "tracked.py": (root / "tracked.py").read_bytes(),
        "my file.py": (root / "my file.py").read_bytes(),
    }
    again = inspect_git_diff(approval, {"mode": "unstaged"})
    assert again.model_dump() == unstaged.model_dump()
    assert (root / ".git" / "index").read_bytes() == before
    assert (root / "tracked.py").read_bytes() == files["tracked.py"]
    assert (root / "my file.py").read_bytes() == files["my file.py"]


def test_restricted_paths_are_filtered(tmp_path):
    root = tmp_path / "ws"
    init_repo(root)
    (root / "keep.py").write_text("value = 1\n", encoding="utf-8")
    git(root, "add", "keep.py")
    git(root, "commit", "-m", "base")
    (root / ".env").write_text("TOKEN=1\n", encoding="utf-8")
    git(root, "add", "-f", ".env")
    (root / ".env").write_text("TOKEN=2\n", encoding="utf-8")

    result = inspect_git_diff(make_approval(root), {"mode": "unstaged"})
    rendered = result.model_dump_json()
    assert result.status == "filtered"
    assert result.entries == []
    assert ".env" not in rendered
    assert "TOKEN" not in rendered


def test_missing_git_baseline_and_denied_path(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.py").write_text("value = 1\n", encoding="utf-8")
    calls = {"n": 0}

    def spy(*_args, **_kwargs):
        calls["n"] += 1
        raise AssertionError("git was invoked")

    monkeypatch.setattr("lean_weave.executor.git_diff.run_bounded", spy)

    missing = inspect_git_diff(make_approval(root), {"mode": "unstaged"})
    assert missing.status == "unavailable"
    denied = inspect_git_diff(make_approval(root), {"mode": "staged", "paths": [".env"]})
    assert denied.status == "denied"
    assert calls["n"] == 0
    (root / ".git").symlink_to(tmp_path / "missing-git")
    dangling = inspect_git_diff(make_approval(root), {"mode": "unstaged"})
    assert dangling.status == "unavailable"
    assert calls["n"] == 0

    monkeypatch.undo()
    (root / ".git").unlink()
    init_repo(root)
    (root / "a.py").write_text("value = 1\n", encoding="utf-8")
    git(root, "add", "a.py")
    unborn_staged = inspect_git_diff(make_approval(root), {"mode": "staged"})
    assert unborn_staged.status == "missing_baseline"
    (root / "a.py").write_text("value = 2\n", encoding="utf-8")
    unborn_unstaged = inspect_git_diff(make_approval(root), {"mode": "unstaged"})
    assert any(item.path == "a.py" for item in unborn_unstaged.entries)


def test_clean_and_truncated_output(tmp_path):
    root = tmp_path / "ws"
    init_repo(root)
    (root / "a.py").write_text("value = 1\n", encoding="utf-8")
    git(root, "add", "a.py")
    git(root, "commit", "-m", "base")

    assert inspect_git_diff(make_approval(root), {"mode": "unstaged"}).status == "clean"
    (root / "a.py").write_text("value = 2\n", encoding="utf-8")
    limited = inspect_git_diff(make_approval(root, capture_bytes_max=8), {"mode": "unstaged"})
    assert limited.status == "incomplete"
    assert limited.entries == []


def test_result_char_budget_truncates_entries(tmp_path):
    root = tmp_path / "ws"
    init_repo(root)
    for index in range(200):
        (root / f"file_{index:03d}.py").write_text("value = 1\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    for index in range(200):
        (root / f"file_{index:03d}.py").write_text("value = 2\n", encoding="utf-8")

    result = inspect_git_diff(make_approval(root, result_chars_max=800), {"mode": "unstaged"})
    assert result.status == "changes"
    assert result.truncated is True
    assert 0 < len(result.entries) < 200
    assert len(result.model_dump_json()) <= 800


def test_scan_limit_is_incomplete(tmp_path):
    root = tmp_path / "ws"
    init_repo(root)
    (root / "a.py").write_text("value = 1\n", encoding="utf-8")
    git(root, "add", "a.py")
    git(root, "commit", "-m", "base")
    (root / "a.py").write_text("value = 2\n", encoding="utf-8")

    result = inspect_git_diff(make_approval(root, scan_bytes_max=1), {"mode": "unstaged"})
    assert result.status == "incomplete"
    assert result.entries == []


def test_git_subprocesses_share_approval_budget(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    init_repo(root)
    (root / "a.py").write_text("value = 1\n", encoding="utf-8")
    git(root, "add", "a.py")
    git(root, "commit", "-m", "base")
    (root / "a.py").write_text("value = 2\n", encoding="utf-8")

    captured = []
    real_run_bounded = git_diff_module.run_bounded

    def spy(argv, *, cwd, env, timeout_s, output_bytes_max):
        captured.append((timeout_s, output_bytes_max))
        return real_run_bounded(argv, cwd=cwd, env=env, timeout_s=timeout_s, output_bytes_max=output_bytes_max)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Git inspection used unbounded subprocess.run")

    monkeypatch.setattr(git_diff_module, "run_bounded", spy)
    monkeypatch.setattr("lean_weave.context.git_metadata.subprocess.run", forbidden)

    result = inspect_git_diff(
        make_approval(root, runtime_s_max=2, capture_bytes_max=4096),
        {"mode": "unstaged"},
    )
    assert result.status == "changes"
    assert captured
    assert all(timeout <= 2 and cap <= 4096 for timeout, cap in captured)
