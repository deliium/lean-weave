"""Shared fixtures for repository-map graph tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from lean_weave.code_index import CodeIndex


def write_tree(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    path = tmp_path / "cache"
    path.mkdir(mode=0o700)
    return path


def build_generation(root: Path, cache: Path, files: dict[str, str]):
    write_tree(root, files)
    index = CodeIndex(root, cache_base=cache)
    index.build()
    return index, index.read_generation()
