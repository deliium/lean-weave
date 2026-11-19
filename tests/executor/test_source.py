import logging

from lean_weave.config.models import AppConfig, ToolsConfig
from lean_weave.executor import read_range, read_symbol, search_symbols
from lean_weave.executor.approval import approve_tools
from tests.executor.conftest import make_approval, write_tree

CANARY = "sk-" + ("d" * 24)
SOURCE = '''\
def deco(fn):
    return fn

@deco
def target():
    """café"""
    return "héllo"

class Box:
    def target(self):
        def nested():
            return 1
        return nested

def target():
    return 2
'''


def test_decorated_nested_duplicate_and_multibyte(tmp_path):
    root = tmp_path / "ws"
    write_tree(root, {"pkg/mod.py": SOURCE, "pkg/other.py": "def other():\n    return 1\n"})
    approval = make_approval(root)

    ranged = read_range(approval, {"path": "pkg/mod.py", "start_line": 6, "end_line": 6})
    assert ranged.ok is True
    assert "café" in ranged.text
    assert ranged.revision.startswith("sha256:")
    decorated = read_symbol(approval, {"path": "pkg/mod.py", "name": "target"})
    # Two module-level targets share no single qualified module prefix match if both end with .target.
    assert decorated.status == "ambiguous"
    assert len(decorated.candidates) >= 2
    assert all(not hasattr(item, "text") for item in decorated.candidates)
    rendered = decorated.model_dump_json()
    assert "return 2" not in rendered
    nested = read_symbol(approval, {"path": "pkg/mod.py", "name": "nested"})
    assert nested.ok is True
    assert nested.text.startswith("        def nested")
    method = read_symbol(approval, {"path": "pkg/mod.py", "name": "Box.target"})
    assert method.ok is True
    assert "def target" in method.text
    assert "def nested" in method.text


def test_exact_symbol_includes_decorator(tmp_path):
    root = tmp_path / "ws"
    write_tree(root, {"mod.py": "@deco\ndef only():\n    return 1\n"})

    result = read_symbol(make_approval(root), {"path": "mod.py", "name": "only"})
    assert result.ok is True
    assert result.start_line == 1
    assert result.text.startswith("@deco\n")
    assert result.end_line == 3


def test_search_order_syntax_and_bounds(tmp_path, caplog):
    root = tmp_path / "ws"
    write_tree(
        root,
        {
            "b.py": "def reader():\n    return 1\n\ndef read():\n    return 2\n",
            "a.py": "def read():\n    return 3\n",
            "bad.py": "def broken(:\n",
        },
    )
    approval = make_approval(root, search_hits_max=10)

    caplog.set_level(logging.DEBUG, logger="lean_weave")
    result = search_symbols(approval, {"query": "read"})
    assert result.ok is True
    assert result.complete is True
    assert "parse_error" in result.omissions
    names = [(hit.path, hit.qualified_name) for hit in result.hits]
    assert names[0][0] == "a.py"
    assert names[0][1].endswith(".read")
    assert any(hit.qualified_name.endswith(".reader") for hit in result.hits)
    assert names.index(next(item for item in names if item[1].endswith(".read") and item[0] == "a.py")) < names.index(
        next(item for item in names if item[1].endswith(".reader"))
    )
    limited = search_symbols(approval, {"query": "read", "hit_limit": 1})
    assert limited.truncated is True
    assert len(limited.hits) == 1
    assert "read" not in caplog.text
    empty = search_symbols(approval, {"query": "missing_name"})
    assert empty.ok is True and empty.hits == [] and empty.complete is True


def test_explicit_scope_does_not_widen(tmp_path):
    root = tmp_path / "ws"
    write_tree(
        root,
        {
            "good.py": "def present():\n    return 1\n",
            ".env": f"TOKEN={CANARY}\n",
        },
    )

    denied = search_symbols(make_approval(root), {"query": "present", "paths": ["good.py", ".env"]})
    rendered = denied.model_dump_json()
    assert denied.ok is False
    assert denied.status == "denied"
    assert "present" not in rendered
    assert CANARY not in rendered
    nested = root / "hidden"
    nested.mkdir()
    (nested / "child.py").write_text("def child():\n    return 1\n", encoding="utf-8")
    (nested / ".gitignore").symlink_to(root / "good.py")
    blocked = search_symbols(make_approval(root), {"query": "child", "paths": ["hidden/child.py"]})
    assert blocked.status == "indeterminate"
    assert "child.py" not in blocked.model_dump_json()


def test_oversized_symbol_offers_range(tmp_path):
    root = tmp_path / "ws"
    write_tree(root, {"mod.py": "def big():\n    return 1\n    return 2\n"})

    result = read_symbol(make_approval(root, range_lines_max=1), {"path": "mod.py", "name": "big"})
    assert result.status == "oversized"
    assert result.start_line == 1
    assert result.end_line == 3
    assert "return 1" not in result.model_dump_json()


def test_line_bounds_and_out_of_range(tmp_path):
    root = tmp_path / "ws"
    write_tree(root, {"mod.py": "one\ntwo\n"})

    approval = make_approval(root)
    assert read_range(approval, {"path": "mod.py", "start_line": 2, "end_line": 1}).status == "invalid"
    assert read_range(approval, {"path": "mod.py", "start_line": 1, "end_line": 3}).status == "invalid"
    ok = read_range(approval, {"path": "mod.py", "start_line": 1, "end_line": 2})
    assert ok.ok is True
    assert ok.text == "one\ntwo\n"


def test_search_marks_uninspected_candidates_incomplete(tmp_path):
    root = tmp_path / "ws"
    write_tree(
        root,
        {
            "good.py": "def present():\n    return 1\n",
            "binary.py": b"\x00\x01\x02def hidden(): pass\n",
            "oversized.py": "def huge():\n" + "    x = 1\n" * 1000,
        },
    )
    approval = make_approval(root, file_bytes_max=200)

    result = search_symbols(approval, {"query": "present"})
    assert result.ok is False
    assert result.status == "incomplete"
    assert result.complete is False
    assert "oversized" in result.omissions
    assert "unsupported" in result.omissions
    assert any(hit.path == "good.py" for hit in result.hits)

    zero = search_symbols(approval, {"query": "missing_name"})
    assert zero.ok is False
    assert zero.status == "incomplete"
    assert zero.complete is False
    assert zero.hits == []


def test_search_respects_result_char_budget(tmp_path):
    root = tmp_path / "ws"
    lines = []
    for index in range(20):
        name = "long_symbol_" + ("x" * 40) + f"_{index}"
        lines.append(f"def {name}():\n    return 1\n")
    (root / "many.py").parent.mkdir(parents=True, exist_ok=True)
    (root / "many.py").write_text("\n".join(lines), encoding="utf-8")
    approval = make_approval(root, result_chars_max=1000, search_hits_max=20)

    result = search_symbols(approval, {"query": "long_symbol"})
    assert result.ok is True
    assert result.truncated is True
    assert len(result.hits) < 20
    assert len(result.model_dump_json()) <= 1000
