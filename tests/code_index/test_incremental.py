from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import lean_weave.code_index.index as index_module
from lean_weave.code_index import CodeIndex
from lean_weave.errors import CodeIndexError


def _git(root: Path, *args: str) -> None:
    env = {
        "PATH": "/usr/bin:/bin",
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)


def test_unchanged_update_parses_nothing(index: CodeIndex):
    index.build()
    update = index.update()
    assert update.count("unchanged") == 3
    assert update.count("parsed") == 0
    assert update.count("syntax_failed") == 0


def test_modified_file_reparses_only_that_file(index: CodeIndex, checkout: Path):
    index.build()
    (checkout / "pkg" / "core.py").write_text("def helper(a, b):\n    return a * b\n")
    update = index.update()
    assert update.count("modified") == 1
    assert update.count("parsed") == 1
    assert update.count("unchanged") == 2


def test_crlf_only_change_does_not_parse(index: CodeIndex, checkout: Path):
    index.build()
    path = checkout / "pkg" / "core.py"
    path.write_bytes(path.read_text().replace("\n", "\r\n").encode())
    update = index.update()
    assert update.count("unchanged") == 3
    assert update.count("parsed") == 0


def test_unique_rename_is_a_move(index: CodeIndex, checkout: Path):
    index.build()
    (checkout / "pkg" / "core.py").rename(checkout / "pkg" / "renamed.py")
    update = index.update()
    assert update.count("moved") == 1
    assert update.count("parsed") == 0
    assert update.count("added") == 0
    assert update.count("removed") == 0


def test_rename_and_edit_is_remove_add(index: CodeIndex, checkout: Path):
    index.build()
    source = checkout / "pkg" / "core.py"
    source.rename(checkout / "pkg" / "renamed.py")
    (checkout / "pkg" / "renamed.py").write_text("def renamed():\n    return 1\n")
    update = index.update()
    assert update.count("added") == 1
    assert update.count("removed") == 1
    assert update.count("moved") == 0


def test_copy_is_not_a_move(index: CodeIndex, checkout: Path):
    index.build()
    source = (checkout / "pkg" / "core.py").read_text()
    (checkout / "pkg" / "copy.py").write_text(source)
    update = index.update()
    assert update.count("added") == 1
    assert update.count("moved") == 0
    assert update.count("unchanged") == 3


def test_delete_removes_rows(index: CodeIndex, checkout: Path):
    index.build()
    (checkout / "pkg" / "broken.py").unlink()
    update = index.update()
    assert update.count("removed") == 1
    assert index.status().files == 2


def test_ignore_transition_purges_prior_rows(index: CodeIndex, checkout: Path):
    index.build()
    (checkout / ".gitignore").write_text("pkg/broken.py\n")
    update = index.update()
    assert update.count("removed") == 1
    assert index.lookup("broken").hits == ()


def test_syntax_outcome_is_cached(index: CodeIndex):
    first = index.build()
    assert first.count("syntax_failed") == 1
    second = index.update()
    assert second.count("parsed") == 0
    assert second.count("syntax_failed") == 0


def test_extractor_change_invalidates_unchanged_content(index: CodeIndex, monkeypatch):
    index.build()
    monkeypatch.setattr(index_module, "extractor_key", lambda: "alt-extractor")
    update = index.update()
    assert update.count("invalidated") == 3
    assert update.count("parsed") == 3
    assert update.count("unchanged") == 3


def test_git_metadata_only_change_keeps_generation(index: CodeIndex, checkout: Path):
    _git(checkout, "init", "-q")
    _git(checkout, "config", "user.email", "fixture@example.invalid")
    _git(checkout, "config", "user.name", "Fixture")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-qm", "fixture")
    index.build()
    _git(checkout, "commit", "--allow-empty", "-qm", "metadata only")
    update = index.update()
    assert update.count("parsed") == 0
    assert update.count("unchanged") == 3


def test_failed_first_build_leaves_no_current_generation(index: CodeIndex, monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("rename failed")

    monkeypatch.setattr(index_module.os, "replace", fail)
    with pytest.raises(CodeIndexError) as exc:
        index.build()
    assert exc.value.code == "storage_unavailable"
    assert index.status().health.value == "missing"
    assert not index._location().pending.exists()


def test_directory_fsync_failure_after_rename_is_uncertain(index: CodeIndex, monkeypatch):
    def fail(_path):
        raise OSError("fsync failed")

    monkeypatch.setattr(index_module, "_fsync_dir", fail)
    with pytest.raises(CodeIndexError) as exc:
        index.build()
    assert exc.value.code == "publication_uncertain"
    assert index._location().database.exists()


def test_stale_pending_file_is_replaced(index: CodeIndex):
    location = index._location()
    directories = [
        location.cache_base / "lean-weave",
        location.cache_base / "lean-weave" / "code-index",
        location.checkout_dir,
    ]
    for directory in directories:
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
    location.pending.write_bytes(b"stale")
    assert index.build().schema_version == 1
    assert not location.pending.exists()
