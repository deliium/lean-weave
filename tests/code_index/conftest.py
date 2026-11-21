"""Fixtures for persistent-index tests: a checkout and an external private cache."""

from __future__ import annotations

from pathlib import Path

import pytest

from lean_weave.code_index import CodeIndex


def write_tree(root: Path, files: dict[str, str | bytes]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    write_tree(
        root,
        {
            "pkg/__init__.py": "",
            "pkg/core.py": (
                "import json\n\n"
                "class Widget:\n"
                "    def render(self, value):\n"
                "        return value\n\n"
                "def helper(a, b):\n"
                "    return a + b\n"
            ),
            "pkg/broken.py": "def broken(:\n",
        },
    )
    return root


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    path = tmp_path / "cache"
    path.mkdir(mode=0o700)
    return path


@pytest.fixture
def index(checkout: Path, cache: Path) -> CodeIndex:
    return CodeIndex(checkout, cache_base=cache)
