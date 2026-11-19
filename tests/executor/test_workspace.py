import os
import stat

from lean_weave.executor.workspace import Workspace, encode_snapshot
from tests.executor.conftest import make_approval, write_tree

CANARY = "sk-" + ("c" * 24)


def test_read_write_scope_and_hard_deny(tmp_path):
    root = tmp_path / "ws"
    write_tree(
        root,
        {
            "a.py": "value = 1\n",
            "notes.txt": "hello\n",
            ".env": f"API_KEY={CANARY}\n",
            ".lean-weave/config.yaml": "version: 1\n",
        },
    )
    approval = make_approval(root, writes=True, write_allow=["a.py"])

    space = Workspace(approval)
    assert space.authorize("a.py") == "ok"
    assert space.authorize("notes.txt") == "ok"
    assert space.authorize("a.py", write=True) == "ok"
    assert space.authorize("notes.txt", write=True) == "denied"
    assert space.authorize(".env") == "hard_denied"
    assert space.authorize(".lean-weave/config.yaml") == "hard_denied"
    loaded = space.read(".env")
    assert loaded == "hard_denied"
    assert CANARY not in str(loaded)


def test_nested_ignore_and_additive_exclude(tmp_path):
    root = tmp_path / "ws"
    write_tree(
        root,
        {
            ".gitignore": "secret.py\n",
            "keep.py": "def keep():\n    return 1\n",
            "secret.py": "def hidden():\n    return 1\n",
            "nested/.gitignore": "*.py\n!visible.py\n",
            "nested/visible.py": "def visible():\n    return 1\n",
            "nested/hidden.py": "def hidden():\n    return 2\n",
        },
    )
    approval = make_approval(root, excludes=["keep.py"])

    space = Workspace(approval)
    found = set(space.discover().files)
    assert "nested/visible.py" in found
    assert "secret.py" not in found
    assert "nested/hidden.py" not in found
    assert "keep.py" not in found
    assert space.authorize("keep.py") == "excluded"


def test_symlink_hardlink_special_and_encoding(tmp_path):
    root = tmp_path / "ws"
    outside = tmp_path / "outside.txt"
    outside.write_text(CANARY, encoding="utf-8")
    root.mkdir()
    (root / "real.py").write_text("def real():\n    return 1\n", encoding="utf-8")
    (root / "link.py").symlink_to(outside)
    nested = root / "sub"
    nested.mkdir()
    (nested / "inner.py").write_text("def inner():\n    return 1\n", encoding="utf-8")
    (root / "alias").symlink_to(nested, target_is_directory=True)
    os.link(root / "real.py", root / "hard.py")
    os.mkfifo(root / "pipe.txt")
    (root / "latin.py").write_bytes("# coding: latin-1\nvalue = '\xe9'\n".encode("latin-1"))
    (root / "bad.py").write_text("# coding: not-a-real-codec\n", encoding="utf-8")
    approval = make_approval(root, writes=True, write_allow=["real.py", "hard.py"])

    space = Workspace(approval)
    assert space.authorize("link.py") == "symlink"
    assert space.read("alias/inner.py") == "symlink"
    assert CANARY not in str(space.read("link.py"))
    loaded = space.read("latin.py")
    assert loaded.encoding == "latin-1"
    assert loaded.lines[1].content == "value = 'é'"
    assert space.read("bad.py") == "undecodable"
    assert not stat.S_ISREG(os.lstat(root / "pipe.txt").st_mode)
    assert space.read("pipe.txt") == "unsupported"
    snapshot = space.read("real.py")
    outcome = space.replace_file(snapshot, snapshot.data)
    assert outcome == "hard_link"
    assert (root / "real.py").read_bytes() == b"def real():\n    return 1\n"


def test_indeterminate_ignore_and_caps(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "ok.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
    nested = root / "nested"
    nested.mkdir()
    (nested / "child.py").write_text("TOKEN_SHOULD_STAY_HIDDEN = 1\n", encoding="utf-8")
    ignore = nested / ".gitignore"
    ignore.write_text("nothing\n", encoding="utf-8")
    ignore.unlink()
    (nested / ".gitignore").symlink_to(root / "ok.py")
    approval = make_approval(root)

    space = Workspace(approval)
    discovery = space.discover()
    assert discovery.complete is False
    assert "indeterminate" in discovery.omissions
    assert "child.py" not in " ".join(discovery.files)
    assert "TOKEN_SHOULD_STAY_HIDDEN" not in str(discovery)
    assert space.read("nested/child.py") == "indeterminate"

    tiny = make_approval(root, file_bytes_max=4, scan_bytes_max=100, visited_entries_max=2)
    limited = Workspace(tiny)
    assert limited.read("ok.py") in {"oversized", "scan_limit"}
    many = Workspace(make_approval(root, visited_entries_max=1))
    found = many.discover()
    assert found.complete is False
    assert "scan_limit" in found.omissions


def test_absent_gitignore_is_normal(tmp_path):
    root = tmp_path / "ws"
    write_tree(root, {"a.py": "def a():\n    return 1\n"})

    discovery = Workspace(make_approval(root)).discover()
    assert discovery.complete is True
    assert discovery.files == ("a.py",)


def test_bytes_round_trip_preserves_bom_and_newlines(tmp_path):
    root = tmp_path / "ws"
    payload = b"\xef\xbb\xbfdef a():\r\n    return 1"
    write_tree(root, {"a.py": payload})

    snapshot = Workspace(make_approval(root)).read("a.py")
    assert snapshot.bom is True
    assert snapshot.newline == "crlf"
    assert snapshot.ends_with_newline is False
    assert encode_snapshot(snapshot.encoding, snapshot.bom, snapshot.lines) == payload
    assert snapshot.revision.startswith("sha256:")


def test_symlinked_ancestor_ignore_is_not_read(tmp_path):
    root = tmp_path / "ws"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "good.py").write_text("def good():\n    return 1\n", encoding="utf-8")
    (outside / ".gitignore").symlink_to(outside / "missing-ignore")
    (root / "alias").symlink_to(outside, target_is_directory=True)

    space = Workspace(make_approval(root))

    assert space.authorize("alias/inner.py") == "symlink"
    assert space.read("alias/inner.py") == "symlink"
