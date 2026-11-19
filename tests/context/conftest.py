"""Reusable deterministic fixture repositories for context tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def write_files(root: Path, files: dict[str, str | bytes]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")


def git(root: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    base = {
        "PATH": "/usr/bin:/bin",
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    }
    if env:
        base.update(env)
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        env=base,
    )


def init_git(root: Path) -> None:
    git(root, "init", "-q")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "Fixture")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "fixture")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def basic_repo(project: Path) -> Path:
    write_files(
        project,
        {
            "pyproject.toml": "[project]\nname='fixture'\nversion='0.1.0'\n",
            "pkg/__init__.py": "",
            "pkg/core.py": (
                "import json\n\n"
                "class Widget:\n"
                "    def render(self, value: str = 'x') -> str:\n"
                "        return json.dumps({'value': value})\n\n"
                "def helper(a, /, b, *args, c=1, **kwargs):\n"
                "    return a + b + c\n"
            ),
            "tests/test_core.py": "from pkg.core import helper\n\ndef test_helper():\n    assert helper(1, 2) == 4\n",
        },
    )
    return project
