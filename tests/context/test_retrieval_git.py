"""Preview-only staged/unstaged retrieval diff evidence."""

from __future__ import annotations

from pathlib import Path

from lean_weave.context.git_metadata import read_retrieval_diff
from lean_weave.verify.process import run_bounded

from .conftest import git, init_git, write_files


def test_clean_repo_returns_empty_frozenset(project: Path):
    write_files(project, {"a.py": "x = 1\n"})
    init_git(project)
    result = read_retrieval_diff(
        project,
        ["a.py"],
        run_capture=run_bounded,
    )
    assert result == frozenset()


def test_unavailable_without_git(project: Path):
    write_files(project, {"a.py": "x = 1\n"})
    assert (
        read_retrieval_diff(project, ["a.py"], run_capture=run_bounded) is None
    )


def test_staged_and_unstaged_intersection(project: Path):
    write_files(project, {"a.py": "x = 1\n", "b.py": "y = 1\n", "c.py": "z = 1\n"})
    init_git(project)
    (project / "a.py").write_text("x = 2\n")
    git(project, "add", "a.py")
    (project / "b.py").write_text("y = 2\n")
    (project / "c.py").unlink()
    (project / "new.py").write_text("n = 1\n")
    result = read_retrieval_diff(
        project,
        ["a.py", "b.py", "c.py", "new.py"],
        run_capture=run_bounded,
    )
    assert result == frozenset({"a.py", "b.py"})


def test_excludes_non_admitted_and_gitfile(project: Path):
    write_files(project, {"a.py": "x = 1\n"})
    init_git(project)
    (project / "a.py").write_text("x = 2\n")
    assert read_retrieval_diff(project, [], run_capture=run_bounded) == frozenset()

    nested = project / "nested"
    nested.mkdir()
    write_files(nested, {"b.py": "y = 1\n"})
    (nested / ".git").write_text(f"gitdir: {project / '.git'}\n")
    assert (
        read_retrieval_diff(nested, ["b.py"], run_capture=run_bounded) is None
    )


def test_unsafe_config_skips_signal(project: Path):
    write_files(project, {"a.py": "x = 1\n"})
    init_git(project)
    git(project, "config", "filter.evil.clean", "true")
    assert read_retrieval_diff(project, ["a.py"], run_capture=run_bounded) is None


def test_malformed_diff_capture_skips_signal(project: Path):
    write_files(project, {"a.py": "x = 1\n"})
    init_git(project)

    def bad_capture(argv, **kwargs):
        from lean_weave.verify.process import ProcessCapture

        if "diff" in argv:
            return ProcessCapture("exited", 0, b"not-nul-framed", b"", 1)
        return run_bounded(argv, **kwargs)

    assert read_retrieval_diff(project, ["a.py"], run_capture=bad_capture) is None
