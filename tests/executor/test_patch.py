import logging
import os

import pytest

from lean_weave.executor import apply_patch, read_range, read_symbol
from lean_weave.executor.models import file_revision
from lean_weave.executor.workspace import Workspace
from tests.executor.conftest import make_approval, snapshot_bytes, write_tree

TEXT = "def one():\n    return 1\n\ndef two():\n    return 2\n"
DIFF = """--- a/mod.py
+++ b/mod.py
@@ -1,2 +1,2 @@
 def one():
-    return 1
+    return 3
@@ -4,2 +4,2 @@
 def two():
-    return 2
+    return 4
"""
BAD_TAIL = """--- a/mod.py
+++ b/mod.py
@@ -1,2 +1,2 @@
 def one():
-    return 1
+    return 3
@@ -4,2 +4,2 @@
 def missing():
-    return 2
+    return 4
"""


def _apply(root, diff, revision, **tools):
    return apply_patch(
        make_approval(root, writes=True, write_allow=["mod.py"], **tools),
        {"path": "mod.py", "expected_revision": revision, "diff": diff},
    )


def test_two_hunks_and_last_hunk_conflict(tmp_path):
    root = tmp_path / "ws"
    write_tree(root, {"mod.py": TEXT})

    approval = make_approval(root, writes=True, write_allow=["mod.py"])
    current = read_range(approval, {"path": "mod.py", "start_line": 1, "end_line": 1})
    original = (root / "mod.py").read_bytes()
    failed = _apply(root, BAD_TAIL, current.revision)
    assert failed.status == "conflict"
    assert (root / "mod.py").read_bytes() == original
    applied = _apply(root, DIFF, current.revision)
    assert applied.ok is True
    assert applied.durability == "confirmed"
    assert applied.lines_added == 2
    assert applied.lines_removed == 2
    assert (root / "mod.py").read_text(encoding="utf-8") == TEXT.replace("return 1", "return 3").replace(
        "return 2", "return 4"
    )
    stale = _apply(root, DIFF, current.revision)
    assert stale.status == "stale"
    assert (root / "mod.py").read_bytes() != original


def test_stale_retry_uses_fresh_read(tmp_path):
    root = tmp_path / "ws"
    write_tree(root, {"mod.py": "def one():\n    return 1\n"})

    approval = make_approval(root, writes=True, write_allow=["mod.py"])
    first = read_symbol(approval, {"path": "mod.py", "name": "one"})
    (root / "mod.py").write_text("def one():\n    return 1\n# touched\n", encoding="utf-8")

    stale = apply_patch(
        approval,
        {
            "path": "mod.py",
            "expected_revision": first.revision,
            "diff": "--- a/mod.py\n+++ b/mod.py\n@@ -1,2 +1,2 @@\n def one():\n-    return 1\n+    return 9\n",
        },
    )
    assert stale.status == "stale"
    assert (root / "mod.py").read_text(encoding="utf-8").endswith("# touched\n")
    fresh = read_symbol(approval, {"path": "mod.py", "name": "one"})
    diff = (
        "--- a/mod.py\n+++ b/mod.py\n@@ -1,3 +1,3 @@\n def one():\n-    return 1\n+    return 9\n # touched\n"
    )
    applied = apply_patch(approval, {"path": "mod.py", "expected_revision": fresh.revision, "diff": diff})
    assert applied.ok is True
    assert "return 9" in (root / "mod.py").read_text(encoding="utf-8")


def test_pre_commit_failures_keep_bytes(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    payload = b"def one():\n    return 1\n"
    write_tree(root, {"mod.py": payload})
    revision = file_revision(payload)
    diff = "--- a/mod.py\n+++ b/mod.py\n@@ -1,2 +1,2 @@\n def one():\n-    return 1\n+    return 2\n"

    def boom(*_args, **_kwargs):
        raise OSError("injected")

    monkeypatch.setattr("lean_weave.executor.workspace._write_all", boom)
    failed = _apply(root, diff, revision)
    assert failed.ok is False
    assert (root / "mod.py").read_bytes() == payload
    assert list(root.glob(".lw-*")) == []

    monkeypatch.undo()
    monkeypatch.setattr(os, "replace", boom)
    failed = _apply(root, diff, revision)
    assert failed.ok is False
    assert (root / "mod.py").read_bytes() == payload
    assert list(root.glob(".lw-*")) == []


def test_durability_warning_after_replace(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    payload = b"def one():\n    return 1\n"
    write_tree(root, {"mod.py": payload})
    calls = {"n": 0}
    real = os.fsync

    def flaky(fd):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("durability")
        return real(fd)

    monkeypatch.setattr(os, "fsync", flaky)
    result = _apply(root, "--- a/mod.py\n+++ b/mod.py\n@@ -1,2 +1,2 @@\n def one():\n-    return 1\n+    return 2\n", file_revision(payload))
    assert result.ok is True
    assert result.durability == "unconfirmed"
    assert (root / "mod.py").read_bytes() == b"def one():\n    return 2\n"


def test_modes_encoding_crlf_and_newline_marker(tmp_path):
    root = tmp_path / "ws"
    payload = b"# coding: latin-1\nvalue = '\xe9'\r\n"
    # Mixed newline is rejected for insertions; use a uniform CRLF latin-1 file.
    payload = "# coding: latin-1\r\nvalue = '\u00e9'\r\n".encode("latin-1")
    (root).mkdir()
    (root / "mod.py").write_bytes(payload)
    os.chmod(root / "mod.py", 0o640)
    diff = (
        "--- a/mod.py\n+++ b/mod.py\n@@ -1,2 +1,2 @@\n # coding: latin-1\n-value = 'é'\n+value = 'ok'\n"
    )
    result = _apply(root, diff, file_revision(payload))
    assert result.ok is True, result.status
    data = (root / "mod.py").read_bytes()
    assert data.startswith(b"# coding: latin-1\r\n")
    assert "ok".encode("latin-1") in data
    assert stat_mode(root / "mod.py") == 0o640

    crlf = b"def one():\r\n    return 1\r\n"
    (root / "mod.py").write_bytes(crlf)
    os.chmod(root / "mod.py", 0o644)
    applied = _apply(
        root,
        "--- a/mod.py\n+++ b/mod.py\n@@ -1,2 +1,2 @@\n def one():\n-    return 1\n+    return 2\n",
        file_revision(crlf),
    )
    assert applied.ok is True
    assert (root / "mod.py").read_bytes() == b"def one():\r\n    return 2\r\n"

    bare = b"def one():\n    return 1"
    (root / "mod.py").write_bytes(bare)
    marked = (
        "--- a/mod.py\n+++ b/mod.py\n@@ -1,2 +1,2 @@\n def one():\n-    return 1\n"
        "\\ No newline at end of file\n+    return 2\n\\ No newline at end of file\n"
    )
    done = _apply(root, marked, file_revision(bare))
    assert done.ok is True, done.status
    assert (root / "mod.py").read_bytes() == b"def one():\n    return 2"

    before = (root / "mod.py").read_bytes()
    contradictory = (
        "--- a/mod.py\n+++ b/mod.py\n@@ -1,2 +1,2 @@\n def one():\n\\ No newline at end of file\n"
        "-    return 2\n+    return 3\n"
    )
    rejected = _apply(root, contradictory, file_revision(before))
    assert rejected.ok is False
    assert (root / "mod.py").read_bytes() == before


def stat_mode(path):
    return os.stat(path).st_mode & 0o777


def test_parent_identity_change_and_permission(tmp_path):
    root = tmp_path / "ws"
    parent = root / "sub"
    parent.mkdir(parents=True)
    target = parent / "mod.py"
    target.write_text("def one():\n    return 1\n", encoding="utf-8")

    approval = make_approval(root, writes=True, write_allow=["sub/mod.py"])
    current = read_range(approval, {"path": "sub/mod.py", "start_line": 1, "end_line": 1})
    diff = "--- a/sub/mod.py\n+++ b/sub/mod.py\n@@ -1,2 +1,2 @@\n def one():\n-    return 1\n+    return 2\n"

    def swap_parent():
        parent.rename(root / "old")
        parent.mkdir()
        target.write_text("def one():\n    return 1\n", encoding="utf-8")

    Workspace.commit_probe = swap_parent
    try:
        result = apply_patch(approval, {"path": "sub/mod.py", "expected_revision": current.revision, "diff": diff})
    finally:
        Workspace.commit_probe = None
    assert result.ok is False
    assert target.read_text(encoding="utf-8") == "def one():\n    return 1\n"

    os.chmod(parent, 0o500)
    try:
        fresh = read_range(approval, {"path": "sub/mod.py", "start_line": 1, "end_line": 1})
        blocked = apply_patch(approval, {"path": "sub/mod.py", "expected_revision": fresh.revision, "diff": diff})
        assert blocked.ok is False
        assert target.read_bytes() == b"def one():\n    return 1\n"
    finally:
        os.chmod(parent, 0o700)


def test_patch_does_not_log_diff(tmp_path, caplog):
    root = tmp_path / "ws"
    write_tree(root, {"mod.py": "def one():\n    return 1\n"})
    caplog.set_level(logging.DEBUG, logger="lean_weave")
    token = "UNIQUE_DIFF_TOKEN"
    _apply(
        root,
        f"--- a/mod.py\n+++ b/mod.py\n@@ -1,2 +1,2 @@\n def one():\n-    return 1\n+    return 1  # {token}\n",
        file_revision((root / "mod.py").read_bytes()),
    )
    assert token not in caplog.text
    assert snapshot_bytes(root)["mod.py"].endswith(b"\n")


def test_eof_insert_after_missing_newline_is_rejected(tmp_path):
    cases = [
        (
            b"last",
            "--- a/mod.py\n+++ b/mod.py\n@@ -1,0 +2,1 @@\n+new\n",
        ),
        (
            b"a\nlast",
            "--- a/mod.py\n+++ b/mod.py\n@@ -2,0 +3,1 @@\n+new\n",
        ),
        (
            b"a\nlast",
            "--- a/mod.py\n+++ b/mod.py\n@@ -2,1 +2,2 @@\n last\n+new\n",
        ),
    ]
    for payload, diff in cases:
        root = tmp_path / "ws"
        write_tree(root, {"mod.py": payload})
        result = _apply(root, diff, file_revision(payload))
        assert result.ok is False
        assert result.status == "unsupported"
        assert (root / "mod.py").read_bytes() == payload
        for path in root.glob(".lw-*"):
            path.unlink()


def test_final_line_without_marker_is_rejected(tmp_path):
    root = tmp_path / "ws"
    payload = b"last"
    write_tree(root, {"mod.py": payload})
    diff = "--- a/mod.py\n+++ b/mod.py\n@@ -1,1 +1,1 @@\n-last\n+changed\n"

    result = _apply(root, diff, file_revision(payload))
    assert result.ok is False
    assert result.status == "unsupported"
    assert (root / "mod.py").read_bytes() == payload


def test_group_writable_mode_is_preserved(tmp_path):
    root = tmp_path / "ws"
    payload = b"def one():\n    return 1\n"
    write_tree(root, {"mod.py": payload})
    os.chmod(root / "mod.py", 0o664)
    previous = os.umask(0o022)
    try:
        result = _apply(
            root,
            "--- a/mod.py\n+++ b/mod.py\n@@ -1,2 +1,2 @@\n def one():\n-    return 1\n+    return 2\n",
            file_revision(payload),
        )
    finally:
        os.umask(previous)
    assert result.ok is True, result.status
    assert stat_mode(root / "mod.py") == 0o664
