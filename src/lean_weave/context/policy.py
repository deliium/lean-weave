"""Eligibility and bounded credential filtering for context retrieval.

This is a documented, high-confidence filter, not a universal secret detector.
Repository content is never opened for default-denied paths, and rejection
messages never echo the supplied text or admitted source.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from lean_weave.errors import ContextScanError

# ---------------------------------------------------------------------------
# Hard denials. Neither a .gitignore negation nor an explicit selection may
# override these. They are checked by path component, so nested occurrences are
# denied too.
# ---------------------------------------------------------------------------

HARD_DENIED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".bzr",
        ".lean-weave",
        ".ai-factory",
        ".opencode",
        ".claude",
        ".cursor",
        ".idea",
        ".vscode",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".venv",
        "venv",
        "node_modules",
        "site-packages",
        "dist",
        "build",
        "target",
        "htmlcov",
        "coverage",
        ".next",
        ".cache",
        ".eggs",
    }
)

HARD_DENIED_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".git-credentials",
        ".gitconfig",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "credentials",
        "credentials.json",
        "secrets.yaml",
        "secrets.yml",
        "secrets.json",
        ".ai-factory.json",
    }
)

HARD_DENIED_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".keystore",
    ".jks",
    ".pyc",
    ".pyo",
    ".so",
    ".dylib",
    ".dll",
    ".o",
    ".a",
    ".class",
    ".jar",
    ".lock",
    ".whl",
    ".tar",
    ".gz",
    ".zip",
    ".min.js",
    ".min.css",
    ".map",
    ".egg-info",
    ".dist-info",
)

HARD_DENIED_PREFIXES = (".env.",)

SUPPORTED_TEXT_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".md",
        ".rst",
        ".txt",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".jsonl",
        ".ini",
        ".cfg",
        ".conf",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".sql",
        ".html",
        ".htm",
        ".css",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".rb",
        ".php",
        ".xml",
        ".svg",
        ".csv",
        ".tsv",
        ".graphql",
        ".proto",
    }
)

SUPPORTED_TEXT_NAMES = frozenset(
    {
        "Makefile",
        "makefile",
        "Dockerfile",
        "Containerfile",
        "README",
        "LICENSE",
        "CHANGELOG",
        "Justfile",
        "justfile",
        "Taskfile",
        "Procfile",
    }
)

PYTHON_SUFFIXES = frozenset({".py", ".pyi"})

# ---------------------------------------------------------------------------
# Credential canaries. These patterns are intentionally high-confidence: the
# goal is to quarantine obvious keys, not to prove a file is secret-free.
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
)

_GENERIC_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|secret(?:[_-]?key)?|access[_-]?token|auth[_-]?token|"
    r"client[_-]?secret|password)\b\s*[:=]\s*['\"]?([A-Za-z0-9_./+=-]{20,})['\"]?"
)


def _looks_like_env_name(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][A-Z0-9_]*", value))


def contains_secret(text: str) -> bool:
    """Return True for high-confidence credential material."""

    if not text:
        return False
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        return True
    for match in _GENERIC_ASSIGNMENT.finditer(text):
        if not _looks_like_env_name(match.group(1)):
            return True
    return False


def is_hard_denied(path: str, *, state_dir: str = ".lean-weave") -> bool:
    """Check hard bans by every path component and by file shape."""

    pure = PurePosixPath(path)
    parts = pure.parts
    if not parts:
        return True
    state_parts = PurePosixPath(state_dir.replace("\\", "/")).parts
    if state_parts and parts[: len(state_parts)] == state_parts:
        return True
    for part in parts[:-1]:
        if part in HARD_DENIED_DIRS:
            return True
    name = parts[-1]
    if name in HARD_DENIED_NAMES:
        return True
    if name.startswith(HARD_DENIED_PREFIXES):
        return True
    lowered = name.lower()
    return lowered.endswith(HARD_DENIED_SUFFIXES)


def is_hard_denied_dir(path: str, *, state_dir: str = ".lean-weave") -> bool:
    """Directory form: every component is subject to the hard-deny set."""

    parts = PurePosixPath(path).parts
    if not parts:
        return True
    state_parts = PurePosixPath(state_dir.replace("\\", "/")).parts
    if state_parts and parts[: len(state_parts)] == state_parts:
        return True
    return any(part in HARD_DENIED_DIRS for part in parts)


def is_supported_text(path: str) -> bool:
    name = PurePosixPath(path).name
    if name in SUPPORTED_TEXT_NAMES:
        return True
    return PurePosixPath(path).suffix.lower() in SUPPORTED_TEXT_SUFFIXES


def is_python(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in PYTHON_SUFFIXES


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def decode_snapshot(data: bytes) -> str | None:
    """Decode a bounded snapshot; return None for binary or undecodable data."""

    if b"\x00" in data:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if text.startswith("\ufeff"):
        text = text[1:]
    return normalize_newlines(text)


def safe_root(root: Path | None = None) -> Path:
    try:
        return (root if root is not None else Path.cwd()).resolve()
    except (OSError, RuntimeError, ValueError):
        raise ContextScanError("Cannot resolve the project root for context retrieval") from None
