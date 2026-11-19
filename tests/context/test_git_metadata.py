from pathlib import Path

from lean_weave.context.git_metadata import read_git_metadata
from lean_weave.context.repository import Repository

from .conftest import git, init_git, write_files


def test_no_git_is_neutral(project: Path):
    write_files(project, {"a.py": "x = 1\n"})
    metadata = read_git_metadata(Repository(project))
    assert metadata.available is False
    assert metadata.reason == "not_a_repository"
    assert metadata.changed == {}


def test_status_staged_unstaged_and_untracked(project: Path):
    write_files(project, {"a.py": "x = 1\n", "b.py": "y = 1\n", "old.py": "z = 1\n"})
    init_git(project)
    (project / "a.py").write_text("x = 2\n")
    git(project, "add", "a.py")
    (project / "b.py").write_text("y = 2\n")
    (project / "new.py").write_text("n = 1\n")
    git(project, "mv", "old.py", "renamed.py")

    metadata = read_git_metadata(Repository(project))
    assert metadata.available is True
    assert "a.py" in metadata.status
    assert "a.py" in metadata.changed
    assert "b.py" in metadata.changed
    assert "new.py" in metadata.changed
    assert "renamed.py" in metadata.status
    assert "old.py" not in metadata.status


def test_recent_commits_are_ordered(project: Path):
    write_files(project, {"a.py": "x = 1\n"})
    init_git(project)
    write_files(project, {"b.py": "y = 1\n"})
    git(project, "add", "b.py")
    git(project, "commit", "-qm", "second")
    metadata = read_git_metadata(Repository(project), recent_commits=2)
    assert metadata.recent["b.py"] == 0
    assert metadata.recent["a.py"] == 1


def test_uncommitted_changes_outrank_committed_positions(project: Path):
    write_files(project, {"a.py": "x = 1\n"})
    init_git(project)
    (project / "a.py").write_text("x = 2\n")
    metadata = read_git_metadata(Repository(project))
    assert metadata.recent["a.py"] == -1


def test_unsafe_filter_configuration_skips_signal(project: Path):
    write_files(project, {"a.py": "x = 1\n"})
    init_git(project)
    canary = project / "canary"
    git(project, "config", "filter.evil.clean", f"sh -c 'touch {canary}'")
    metadata = read_git_metadata(Repository(project))
    assert metadata.available is False
    assert metadata.reason == "unsafe_config"
    assert not canary.exists()


def test_git_commands_do_not_mutate_worktree(project: Path):
    write_files(project, {"a.py": "x = 1\n"})
    init_git(project)
    (project / "a.py").write_text("x = 2\n")
    before = git(project, "status", "--porcelain=v1").stdout
    read_git_metadata(Repository(project))
    after = git(project, "status", "--porcelain=v1").stdout
    assert before == after
