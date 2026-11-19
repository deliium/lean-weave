from pathlib import Path

from lean_weave.context.git_metadata import GitMetadata
from lean_weave.context.models import ContextQuery, ExpansionLevel
from lean_weave.context.python_index import build_python_index
from lean_weave.context.ranking import rank, tokenize
from lean_weave.context.repository import Repository

from .conftest import write_files


def _ranked(project: Path, query: ContextQuery, metadata=None, include_tests=False):
    repository = Repository(project)
    index = build_python_index(repository)
    return rank(repository, index, query, metadata=metadata, include_tests=include_tests)


def test_tokenizer_is_deterministic():
    assert tokenize("Create_LLM provider, create_llm_provider") == [
        "create_llm",
        "llm",
        "provider",
        "create_llm_provider",
    ]


def test_exact_symbol_collision_breaks_by_path(project: Path):
    write_files(
        project,
        {
            "pkg/wanted.py": "def wanted():\n    return 1\n",
            "other/wanted.py": "def wanted():\n    return 2\n",
        },
    )
    query = ContextQuery(task="wanted", symbols=["wanted"], level=ExpansionLevel.SYMBOLS)
    ranked = _ranked(project, query)
    assert [item.path for item in ranked.symbols] == ["other/wanted.py", "pkg/wanted.py"]


def test_keyword_repetition_alone_does_not_qualify(project: Path):
    write_files(
        project,
        {
            "target.py": "def target():\n    return 1\n",
            "noise.py": "# target target target target\nVALUE = 1\n",
        },
    )
    ranked = _ranked(project, ContextQuery(task="target"))
    assert [item.path for item in ranked.files] == ["target.py"]


def test_explicit_path_and_one_hop_relationship(project: Path):
    write_files(
        project,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "from .b import helper\n\ndef a():\n    return helper()\n",
            "pkg/b.py": "def helper():\n    return 1\n",
        },
    )
    ranked = _ranked(project, ContextQuery(task="a work", paths=["pkg/a.py"]))
    by_path = {item.path: item for item in ranked.files}
    assert by_path["pkg/a.py"].explicit_path == 1
    assert by_path["pkg/b.py"].import_related == 1
    assert ranked.related_paths == ["pkg/b.py"]


def test_recent_change_alone_never_qualifies(project: Path):
    write_files(
        project,
        {
            "target.py": "def target():\n    return 1\n",
            "unrelated.py": "VALUE = 1\n",
        },
    )
    metadata = GitMetadata(available=True, recent={"unrelated.py": 0})
    ranked = _ranked(project, ContextQuery(task="target"), metadata=metadata)
    assert [item.path for item in ranked.files] == ["target.py"]


def test_ties_and_inferred_path_are_stable(project: Path):
    write_files(
        project,
        {
            "b/alpha.py": "VALUE = 1\n",
            "a/alpha.py": "VALUE = 1\n",
        },
    )
    ranked = _ranked(project, ContextQuery(task="alpha"))
    assert [item.path for item in ranked.files] == ["a/alpha.py", "b/alpha.py"]


def test_matching_tests_only_when_enabled(project: Path):
    write_files(
        project,
        {
            "pkg/core.py": "def core():\n    return 1\n",
            "tests/test_core.py": "def test_core():\n    assert True\n",
        },
    )
    query = ContextQuery(task="core", paths=["pkg/core.py"])
    without = _ranked(project, query)
    with_tests = _ranked(project, query, include_tests=True)
    assert "tests/test_core.py" not in {item.path for item in without.files}
    assert "tests/test_core.py" in {item.path for item in with_tests.files}
