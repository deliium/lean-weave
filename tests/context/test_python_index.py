from pathlib import Path

import pytest

from lean_weave.context.models import SymbolKind, content_digest
from lean_weave.context.python_index import (
    build_python_index,
    extract_python_facts,
    project_python_facts,
)
from lean_weave.context.repository import FileSnapshot, Repository

from .conftest import write_files


@pytest.fixture
def indexed(project: Path):
    write_files(
        project,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": (
                "from . import b\n"
                "from .b import thing\n"
                "import pkg.c\n\n"
                "@decorator\n"
                "class Alpha:\n"
                "    @staticmethod\n"
                "    async def method(self, value: str = 'é', /, *, flag: bool = False) -> int:\n"
                "        def nested(x):\n"
                "            return x\n"
                "        return 1\n\n"
                "async def top(a, /, b, *args, c=1, **kwargs):\n"
                "    return a\n"
            ),
            "pkg/b.py": "from .a import Alpha\nthing = 1\n",
            "pkg/c.py": "VALUE = 1\n",
            "pkg/broken.py": "def broken(:\n",
        },
    )
    repository = Repository(project)
    return repository, build_python_index(repository)


def test_lookup_by_name_and_file(indexed):
    repository, index = indexed
    matches = index.lookup("method")
    assert len(matches) == 1
    assert matches[0].qualified_name == "pkg.a.Alpha.method"
    assert matches[0].kind == SymbolKind.ASYNC_METHOD
    assert index.lookup("method", path="pkg/b.py") == []
    assert index.lookup("pkg.a.Alpha")[0].kind == SymbolKind.CLASS
    assert index.lookup("Alpha")[0].simple_name == "Alpha"


def test_decorated_span_and_signature(indexed):
    _, index = indexed
    alpha = index.lookup("Alpha")[0]
    assert alpha.start_line == 5
    assert alpha.end_line == 11
    method = index.lookup("method")[0]
    assert method.start_line == 7
    assert "async def method" in method.signature
    assert "/" in method.signature
    assert "*" in method.signature
    assert "-> int" in method.signature
    assert "é" in method.signature


def test_nested_and_top_level(indexed):
    _, index = indexed
    nested = index.lookup("nested")[0]
    assert nested.qualified_name.endswith("Alpha.method.nested")
    top = index.lookup("top")[0]
    assert top.kind == SymbolKind.ASYNC_FUNCTION
    assert top.qualified_name == "pkg.a.top"


def test_imports_are_resolved_and_cycles_are_finite(indexed):
    _, index = indexed
    assert index.import_edges["pkg/a.py"] == (
        "pkg/__init__.py",
        "pkg/b.py",
        "pkg/c.py",
    )
    assert index.import_edges["pkg/b.py"] == ("pkg/a.py",)
    assert index.one_hop(["pkg/a.py"]) == ["pkg/__init__.py", "pkg/b.py", "pkg/c.py"]
    assert index.one_hop(["pkg/a.py", "pkg/b.py"]) == [
        "pkg/__init__.py",
        "pkg/a.py",
        "pkg/b.py",
        "pkg/c.py",
    ]


def test_broken_source_does_not_lose_unrelated_files(indexed):
    _, index = indexed
    assert index.errors == {"pkg/broken.py": "syntax"}
    assert index.lookup("top")
    assert index.lookup("Alpha")


def test_duplicate_symbols_are_preserved(project: Path):
    write_files(
        project,
        {
            "a.py": "def same():\n    return 1\n",
            "b.py": "def same():\n    return 2\n",
        },
    )
    index = build_python_index(Repository(project))
    matches = index.lookup("same")
    assert len(matches) == 2
    assert [item.path for item in matches] == ["a.py", "b.py"]


def test_ambiguous_module_names_are_not_resolved(project: Path):
    write_files(
        project,
        {
            "foo.py": "def root(): pass\n",
            "src/foo.py": "def src(): pass\n",
            "consumer.py": "import foo\n",
        },
    )
    index = build_python_index(Repository(project))
    assert "foo" not in index.module_paths
    assert index.import_edges["consumer.py"] == ()


def test_relative_import_without_package_is_ignored(project: Path):
    write_files(project, {"loose.py": "from . import missing\n"})
    index = build_python_index(Repository(project))
    assert index.import_edges["loose.py"] == ()


def _snapshot(path: str, text: str) -> FileSnapshot:
    return FileSnapshot(path, text, content_digest(text), len(text.encode("utf-8")))


def test_facts_capture_decorators_signatures_and_occurrences():
    text = (
        "@one\n"
        "@two(arg=1)\n"
        "class C:\n"
        "    @staticmethod\n"
        "    async def run(self, a, /, b, *, c=1) -> int:\n"
        "        return 1\n"
        "\n"
        "def same():\n"
        "    return 1\n"
        "def same():\n"
        "    return 2\n"
    )
    facts = extract_python_facts(_snapshot("pkg/a.py", text))
    assert facts.status == "ok"
    assert facts.definitions[0].decorators == ("one", "two(arg=1)")
    assert facts.definitions[0].kind == SymbolKind.CLASS
    assert facts.definitions[0].start_line == 1
    run = next(item for item in facts.definitions if item.simple_name == "run")
    assert run.kind == SymbolKind.ASYNC_METHOD
    assert run.start_line == 4
    assert run.end_line == 6
    assert "/" in run.signature and "*" in run.signature and "-> int" in run.signature
    duplicates = [item for item in facts.definitions if item.simple_name == "same"]
    assert [item.occurrence for item in duplicates] == [0, 1]
    assert all(item.local_name == "same" for item in duplicates)


def test_facts_support_unicode_empty_and_pyi():
    facts = extract_python_facts(_snapshot("pkg/é.py", "def café(значение):\n    return значение\n"))
    assert facts.definitions[0].simple_name == "café"
    stub = extract_python_facts(_snapshot("pkg/api.pyi", "def declared(x: int) -> str: ...\n"))
    assert stub.status == "ok"
    assert stub.definitions[0].signature == "def declared(x: int) -> str"
    empty = extract_python_facts(_snapshot("pkg/empty.py", ""))
    assert empty.status == "ok"
    assert empty.line_count == 0


def test_failed_facts_have_no_definitions_or_imports():
    facts = extract_python_facts(_snapshot("pkg/broken.py", "import x\ndef broken(:\n"))
    assert facts.status == "syntax"
    assert facts.definitions == ()
    assert facts.imports == ()


def test_imports_are_statement_records():
    facts = extract_python_facts(
        _snapshot("pkg/a.py", "import a, b as bee\nfrom . import c\nfrom .d import e as f\nfrom .d import *\n")
    )
    assert [item.kind for item in facts.imports] == ["import", "from", "from", "from"]
    assert facts.imports[0].names == (("a", None), ("b", "bee"))
    assert facts.imports[1].level == 1
    assert facts.imports[2].names == (("e", "f"),)
    assert facts.imports[3].names == (("*", None),)


def test_move_projection_uses_new_path_and_module():
    facts = extract_python_facts(_snapshot("pkg/a.py", "def f():\n    return 1\n"))
    moved = project_python_facts("other/b.py", facts)
    assert moved.path == "other/b.py"
    assert moved.module == "other.b"
    assert moved.symbols[0].qualified_name == "other.b.f"
    assert facts.definitions[0].local_name == "f"


def test_load_facts_callback_reuses_one_parse(project: Path):
    write_files(project, {"a.py": "def f():\n    return 1\n"})
    repository = Repository(project)
    calls: list[str] = []

    def load(snapshot):
        calls.append(snapshot.path)
        return extract_python_facts(snapshot)

    index = build_python_index(repository, load_facts=load)
    assert calls == ["a.py"]
    assert index.lookup("f")
