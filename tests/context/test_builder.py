from pathlib import Path

import pytest

from lean_weave.config import ContextConfig
from lean_weave.context.builder import ContextBuilder
from lean_weave.context.models import ContextQuery, ExpansionLevel, OmissionReason
from lean_weave.context.repository import Repository
from lean_weave.errors import ContextBudgetError
from lean_weave.metrics.budget import estimate_tokens

from .conftest import write_files

CORE = (
    "class Widget:\n"
    "    def render(self, value: str = 'é') -> str:\n"
    "        return value\n\n"
    "def helper(a, /, b, *args, c=1, **kwargs):\n"
    "    return a + b + c\n\n"
    "def long_unit():\n"
    "    return '" + "x" * 80 + "'\n"
)


@pytest.fixture
def rich_repo(project: Path):
    write_files(
        project,
        {
            "pkg/__init__.py": "",
            "pkg/core.py": CORE,
            "pkg/mirror.py": "def same():\n    return 1\n",
            "pkg/other.py": "def same():\n    return 1\n",
            "tests/test_core.py": "def test_core():\n    assert True\n",
        },
    )
    return project


def _build(project: Path, query: ContextQuery, config: ContextConfig | None = None):
    repository = Repository(project, config or ContextConfig())
    return ContextBuilder(repository, config=config).build(query)


def test_render_equals_estimator(rich_repo: Path):
    result = _build(
        rich_repo,
        ContextQuery(task="helper", level=ExpansionLevel.SYMBOLS, paths=["pkg/core.py"]),
    )
    assert result.pack.estimated_tokens == estimate_tokens(result.messages)
    assert result.pack.estimated_tokens == result.pack.estimated_tokens
    assert len(result.messages) == 2
    assert result.messages[0]["role"] == "system"


def test_progressive_expansion(rich_repo: Path):
    map_result = _build(
        rich_repo,
        ContextQuery(task="helper", level=ExpansionLevel.MAP, paths=["pkg/core.py"]),
    )
    symbols_result = _build(
        rich_repo,
        ContextQuery(task="helper", level=ExpansionLevel.SYMBOLS, paths=["pkg/core.py"]),
    )
    ranges_result = _build(
        rich_repo,
        ContextQuery(task="helper", level=ExpansionLevel.RANGES, paths=["pkg/core.py"]),
    )
    full_result = _build(
        rich_repo,
        ContextQuery(
            task="helper",
            level=ExpansionLevel.FULL,
            paths=["pkg/core.py"],
            reason="whole file required",
        ),
    )
    assert map_result.pack.excerpts == []
    assert map_result.pack.repository_map
    assert symbols_result.pack.symbols
    assert symbols_result.pack.excerpts == []
    assert ranges_result.pack.excerpts
    assert any(item.kind == "range" for item in ranges_result.pack.excerpts)
    assert full_result.pack.excerpts[0].kind == "full"
    assert full_result.pack.excerpts[0].text == CORE
    for result in (map_result, symbols_result, ranges_result, full_result):
        assert result.pack.estimated_tokens > 0


def test_source_slice_and_provenance(rich_repo: Path):
    repository = Repository(rich_repo)
    snapshot = repository.read("pkg/core.py")
    result = _build(
        rich_repo,
        ContextQuery(
            task="helper",
            level=ExpansionLevel.RANGES,
            paths=["pkg/core.py"],
            symbols=["helper"],
            exact_symbols_only=True,
        ),
    )
    excerpt = next(item for item in result.pack.excerpts if item.symbol == "pkg.core.helper")
    lines = snapshot.text.split("\n")
    expected = "\n".join(lines[excerpt.start_line - 1 : excerpt.end_line])
    assert excerpt.text == expected
    assert excerpt.digest == snapshot.digest
    assert excerpt.provenance[0].start_line == 5
    assert excerpt.provenance[0].end_line == 6


def test_identical_code_is_stored_once_with_alternate_provenance(rich_repo: Path):
    result = _build(
        rich_repo,
        ContextQuery(
            task="same",
            level=ExpansionLevel.RANGES,
            paths=["pkg/mirror.py", "pkg/other.py"],
        ),
    )
    same = [item for item in result.pack.excerpts if item.text.strip().startswith("def same")]
    assert len(same) == 1
    assert len(same[0].provenance) == 2
    assert {item.path for item in same[0].provenance} == {"pkg/mirror.py", "pkg/other.py"}


def test_same_file_ranges_do_not_overlap(rich_repo: Path):
    result = _build(
        rich_repo,
        ContextQuery(task="Widget helper", level=ExpansionLevel.RANGES, paths=["pkg/core.py"]),
    )
    ranges = [item for item in result.pack.excerpts if item.path == "pkg/core.py"]
    for left in ranges:
        for right in ranges:
            if left is right:
                continue
            assert left.end_line < right.start_line or right.end_line < left.start_line


def test_full_file_falls_back_when_over_per_file_cap(rich_repo: Path):
    config = ContextConfig(per_file_chars_max=20)
    result = _build(
        rich_repo,
        ContextQuery(
            task="core",
            level=ExpansionLevel.FULL,
            paths=["pkg/core.py"],
            reason="whole file required",
        ),
        config=config,
    )
    assert all(item.kind != "full" for item in result.pack.excerpts)
    assert any(item.reason == OmissionReason.BUDGET for item in result.pack.omissions)
    assert all(len(item.text) <= 20 for item in result.pack.excerpts)


def test_required_envelope_overflow(rich_repo: Path):
    config = ContextConfig(stage_prompt_tokens_max=10)
    with pytest.raises(ContextBudgetError):
        _build(
            rich_repo,
            ContextQuery(task="core", level=ExpansionLevel.MAP),
            config=config,
        )


def test_retry_excerpt_cap(rich_repo: Path):
    config = ContextConfig(retry_excerpt_chars_max=10)
    result = _build(
        rich_repo,
        ContextQuery(
            task="core",
            level=ExpansionLevel.RANGES,
            paths=["pkg/core.py"],
            stage="retry",
        ),
        config=config,
    )
    assert sum(len(item.text) for item in result.pack.excerpts) <= 10


def test_scope_prevents_unselected_source(rich_repo: Path):
    result = _build(
        rich_repo,
        ContextQuery(
            task="same core",
            level=ExpansionLevel.RANGES,
            paths=["pkg/core.py"],
            scope_paths=["pkg/core.py"],
        ),
    )
    assert all(item.path == "pkg/core.py" for item in result.pack.excerpts)
    assert all(item.path == "pkg/core.py" for item in result.pack.symbols)


def test_unicode_round_trip(rich_repo: Path):
    result = _build(
        rich_repo,
        ContextQuery(task="é helper", level=ExpansionLevel.RANGES, paths=["pkg/core.py"]),
    )
    assert "é" in result.user_prompt
    assert result.pack.estimated_tokens == estimate_tokens(result.messages)


def test_empty_full_file_reports_stable_omission(project: Path):
    write_files(project, {"empty.py": ""})
    result = _build(
        project,
        ContextQuery(
            task="empty",
            level=ExpansionLevel.FULL,
            paths=["empty.py"],
            reason="review the complete file",
        ),
    )
    assert result.pack.excerpts == []
    assert any(item.reason == OmissionReason.EMPTY for item in result.pack.omissions)


def test_over_budget_range_keeps_signature(project: Path):
    write_files(
        project,
        {
            "big.py": "def big():\n"
            + "".join(f"    x{index} = {index}\n" for index in range(50))
            + "    return 1\n"
        },
    )
    result = _build(
        project,
        ContextQuery(task="big", level=ExpansionLevel.RANGES, paths=["big.py"]),
        config=ContextConfig(per_file_chars_max=30),
    )
    assert result.pack.excerpts == []
    assert any(item.simple_name == "big" for item in result.pack.symbols)
    assert "def big()" in result.user_prompt
    assert any(item.reason == OmissionReason.BUDGET for item in result.pack.omissions)


def test_contained_range_does_not_duplicate_signature(rich_repo: Path):
    result = _build(
        rich_repo,
        ContextQuery(task="Widget", level=ExpansionLevel.RANGES, paths=["pkg/core.py"]),
    )
    assert all(
        item.qualified_name != "pkg.core.Widget.render" for item in result.pack.symbols
    )
    assert result.user_prompt.count("def render") == 1


def test_pack_symbols_only_include_admitted_symbols(rich_repo: Path):
    result = _build(
        rich_repo,
        ContextQuery(task="Widget helper", level=ExpansionLevel.RANGES, paths=["pkg/core.py"]),
        config=ContextConfig(per_file_chars_max=5),
    )
    assert result.pack.symbols == []


def test_repository_map_is_scoped_and_omits_secret_paths(project: Path):
    write_files(
        project,
        {
            "pkg/core.py": "def core():\n    return 1\n",
            "pkg/other.py": "def other():\n    return 2\n",
            "leak.py": "KEY = '" + "sk-" + "e" * 32 + "'\n",
        },
    )
    result = _build(
        project,
        ContextQuery(
            task="core",
            level=ExpansionLevel.MAP,
            paths=["pkg/core.py"],
            scope_paths=["pkg/core.py"],
        ),
    )
    assert result.pack.repository_map == ["pkg/core.py [pkg.core] symbols=1"]
    assert "leak.py" not in result.user_prompt
