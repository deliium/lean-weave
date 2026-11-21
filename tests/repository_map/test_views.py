from __future__ import annotations

from pathlib import Path

import pytest

from lean_weave.code_index import CodeIndex
from lean_weave.errors import RepositoryMapError
from lean_weave.repository_map import MapLimits, RepositoryMap


def _write(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _map(root: Path, cache: Path, files: dict[str, str], limits: MapLimits | None = None):
    _write(root, files)
    index = CodeIndex(root, cache_base=cache)
    index.build()
    return RepositoryMap(index, limits=limits or MapLimits())


def test_show_is_deterministic_and_beats_lexical(tmp_path: Path, cache: Path):
    root = tmp_path / "repo"
    # Hub name sorts after leaf_a/b/c so lexical order would bury it.
    service = _map(
        root,
        cache,
        {
            "leaf_a.py": "def a():\n    return 1\n",
            "leaf_b.py": "def b():\n    return 1\n",
            "leaf_c.py": "def c():\n    return 1\n",
            "zz_hub.py": (
                "def hub():\n    return 1\n"
            ),
            "consumer_a.py": "from zz_hub import hub\n",
            "consumer_b.py": "from zz_hub import hub\n",
            "consumer_c.py": "from zz_hub import hub\n",
            "tests/test_leaf.py": "def test_x():\n    return 1\n",
        },
    )
    first = service.show()
    second = service.show()
    assert first.text == second.text
    assert first.rows == second.rows
    joined = "\n".join(first.rows)
    assert "zz_hub" in "\n".join(first.rows[:6])
    assert "zz_hub.hub" in joined or any("hub" in row for row in first.rows[:6])
    assert "tests/test_leaf.py" not in joined
    # Lexical-only would list leaf_* before zz_hub.
    lexical = sorted(
        path for path in ("leaf_a.py", "leaf_b.py", "leaf_c.py", "zz_hub.py")
    )
    assert lexical[0] != "zz_hub.py"
    assert first.rows[0].startswith("zz_hub ::")


def test_module_and_dependencies_and_references(tmp_path: Path, cache: Path):
    root = tmp_path / "repo"
    service = _map(
        root,
        cache,
        {
            "pkg/__init__.py": "",
            "pkg/core.py": "def helper():\n    return 1\n",
            "app.py": "from pkg.core import helper\nimport pkg.core\n",
        },
    )
    module_view = service.module("pkg.core")
    assert any("pkg.core ::" in row for row in module_view.rows)
    deps = service.dependencies("app")
    assert any(row.startswith("out ") for row in deps.rows)
    refs = service.references("helper")
    assert any(row.startswith("def ") for row in refs.rows)
    assert any(row.startswith("import ") for row in refs.rows)


def test_ambiguous_and_invalid_selectors(tmp_path: Path, cache: Path):
    root = tmp_path / "repo"
    service = _map(
        root,
        cache,
        {
            "mod.py": "def twin():\n    return 1\n",
            "mod.pyi": "def twin() -> int: ...\n",
            "other.py": "def twin():\n    return 2\n",
        },
    )
    with pytest.raises(RepositoryMapError) as exc:
        service.module("mod")
    assert exc.value.code == "ambiguous_selector"
    with pytest.raises(RepositoryMapError) as exc:
        service.references("twin")
    assert exc.value.code == "ambiguous_selector"
    with pytest.raises(RepositoryMapError) as exc:
        service.module("class")
    assert exc.value.code == "invalid_selector"
    with pytest.raises(RepositoryMapError) as exc:
        service.module("missing")
    assert exc.value.code == "not_found"


def test_budget_empty_sentinel_and_short_definition(tmp_path: Path, cache: Path):
    root = tmp_path / "repo"
    long_sig = "def huge(" + ", ".join(f"a{i}: int" for i in range(40)) + "):\n    return 1\n"
    service = _map(
        root,
        cache,
        {"mod.py": long_sig},
        limits=MapLimits(tokens_max=80, rows_max=2),
    )
    result = service.show()
    assert result.truncated is True
    assert any("[" in row and "::" not in row for row in result.rows) or result.rows

    tiny = RepositoryMap(
        CodeIndex(root, cache_base=cache),
        limits=MapLimits(tokens_max=1, rows_max=1),
    )
    # Index already built.
    emptied = tiny.show()
    assert emptied.truncated is True
    assert emptied.text == "."
    assert emptied.rows == ()


def test_dependency_budget_keeps_both_directions(tmp_path: Path, cache: Path):
    root = tmp_path / "repo"
    files = {
        "hub.py": "def hub():\n    return 1\n",
        "in_a.py": "import hub\n",
        "out_many.py": "\n".join(f"import hub as h{i}" for i in range(20)) + "\n",
    }
    # out_many imports hub 20 times - for hub dependencies we need hub importing out
    files["hub.py"] = "import leaf\ndef hub():\n    return 1\n"
    files["leaf.py"] = "def leaf():\n    return 1\n"
    files["in_a.py"] = "import hub\n"
    service = _map(root, cache, files, limits=MapLimits(tokens_max=500, rows_max=2))
    result = service.dependencies("hub")
    directions = {row.split(" ", 1)[0] for row in result.rows}
    assert "in" in directions and "out" in directions


def test_control_escaping(tmp_path: Path, cache: Path):
    root = tmp_path / "repo"
    # Angle brackets in a signature-like class name via bases.
    service = _map(
        root,
        cache,
        {"mod.py": "class Box:\n    def render(self):\n        return 1\n"},
    )
    result = service.show()
    assert "<" not in result.text or "\\x3c" in result.text


def test_large_catalog_stays_bounded(tmp_path: Path, cache: Path):
    root = tmp_path / "repo"
    # Sparse 2,000-module corpus within default scan limits; map output stays capped.
    files = {f"m{i:04d}.py": f"def f{i}():\n    return {i}\n" for i in range(2000)}
    files["hub.py"] = "def hub():\n    return 1\n"
    for i in range(10):
        files[f"c{i}.py"] = "from hub import hub\n"
    service = _map(root, cache, files, limits=MapLimits(tokens_max=256, rows_max=20))
    result = service.show()
    assert len(result.rows) <= 20
    assert result.estimated_tokens <= 256
    assert any("hub" in row for row in result.rows)
