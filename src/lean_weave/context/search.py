"""Equivalent literal-search backends over canonical in-memory snapshots.

``rg`` never receives repository paths: every logical line is streamed on
stdin and a parallel table maps stream line numbers back to project paths.
Any backend problem discards partial results and falls back to Python matching
over exactly the same lines.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field

from lean_weave.context.models import iter_lines
from lean_weave.context.repository import Repository, FileSnapshot
from lean_weave.errors import InvalidContextRequest
from lean_weave.logging_setup import get_logger

log = get_logger("context.search")

RG_TIMEOUT_S = 10.0
RG_OUTPUT_LIMIT = 4 * 1024 * 1024
MAX_QUERY_CHARS = 2000


@dataclass(frozen=True)
class SearchHit:
    path: str
    line: int
    column: int
    text: str


@dataclass
class SearchResult:
    hits: list[SearchHit] = field(default_factory=list)
    backend: str = "python"
    degraded_reason: str | None = None
    truncated: bool = False


def validate_literal(query: str) -> str:
    if not isinstance(query, str) or not query:
        raise InvalidContextRequest("Search query must be non-empty")
    if "\x00" in query or "\n" in query or "\r" in query:
        raise InvalidContextRequest("Search query must be a single line without NUL bytes")
    if len(query) > MAX_QUERY_CHARS:
        raise InvalidContextRequest("Search query is too long")
    return query


def _snapshots(repository: Repository) -> tuple[list[FileSnapshot], list[str]]:
    snapshots: list[FileSnapshot] = []
    skipped: list[str] = []
    for path in repository.inventory().files:
        snapshot = repository.try_read(path)
        if snapshot is None:
            skipped.append(path)
            continue
        snapshots.append(snapshot)
    return snapshots, skipped


def _line_table(snapshots: list[FileSnapshot]) -> list[tuple[str, int, str]]:
    table: list[tuple[str, int, str]] = []
    for snapshot in snapshots:
        for number, line in enumerate(iter_lines(snapshot.text), start=1):
            table.append((snapshot.path, number, line))
    return table


def _payload(table: list[tuple[str, int, str]]) -> bytes:
    # No separators between files: every entry contributes exactly one LF.
    return b"".join(line.encode("utf-8") + b"\n" for _, _, line in table)


def _rg_environment() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "HOME": os.devnull,
        "XDG_CONFIG_HOME": os.devnull,
    }


def _run_rg(query: str, payload: bytes) -> tuple[list[tuple[int, int]], str | None, bool]:
    """Return ``(matches, degraded_reason, no_matches)``."""

    executable = shutil.which("rg")
    if executable is None:
        return [], "rg_missing", False
    argv = [
        executable,
        "--no-config",
        "--fixed-strings",
        "--json",
        "--no-mmap",
        "--",
        query,
    ]
    try:
        completed = subprocess.run(
            argv,
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=RG_TIMEOUT_S,
            check=False,
            shell=False,
            env=_rg_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        return [], "rg_error", False
    if completed.returncode == 1:
        return [], None, True
    if completed.returncode != 0:
        return [], "rg_exit", False
    if len(completed.stdout) > RG_OUTPUT_LIMIT:
        return [], "rg_overflow", False
    matches: list[tuple[int, int]] = []
    for raw in completed.stdout.splitlines():
        try:
            event = json.loads(raw)
        except (ValueError, TypeError):
            return [], "rg_malformed", False
        if not isinstance(event, dict) or event.get("type") != "match":
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            return [], "rg_malformed", False
        number = data.get("line_number")
        submatches = data.get("submatches") or []
        offset = 0
        if isinstance(submatches, list) and submatches:
            maybe = submatches[0].get("start") if isinstance(submatches[0], dict) else None
            if isinstance(maybe, int) and maybe >= 0:
                offset = maybe
        if not isinstance(number, int) or number < 1:
            return [], "rg_malformed", False
        matches.append((number, offset))
    return matches, None, completed.returncode == 1


def search(repository: Repository, query: str, *, limit: int | None = None) -> SearchResult:
    literal = validate_literal(query)
    effective_limit = limit if limit is not None else repository.config.search_hits_max
    if effective_limit <= 0:
        raise InvalidContextRequest("Search limit must be positive")
    if effective_limit > repository.config.search_hits_max:
        raise InvalidContextRequest("Search limit cannot exceed context.search_hits_max")

    snapshots, _ = _snapshots(repository)
    table = _line_table(snapshots)
    result = SearchResult()
    degraded: str | None = None

    matches, reason, _no_matches = _run_rg(literal, _payload(table))
    if reason is not None:
        degraded = reason
        matches = []
    else:
        # Validate every rg index before trusting any result.
        if any(number < 1 or number > len(table) for number, _ in matches):
            degraded = "rg_malformed"
            matches = []
        elif any(literal not in table[number - 1][2] for number, _ in matches):
            degraded = "rg_malformed"
            matches = []

    if degraded is None and matches:
        result.backend = "rg"
        hits: list[SearchHit] = []
        for number, byte_offset in matches:
            path, line_number, text = table[number - 1]
            column = text.find(literal)
            if column < 0:
                column = 0
            hits.append(SearchHit(path=path, line=line_number, column=column, text=text))
        result.hits = sorted(hits, key=lambda item: (item.path, item.line, item.column))
    elif degraded is None:
        # rg succeeded (exit 0 or 1) with zero matches.
        result.backend = "rg"
        result.hits = []
    else:
        # Python substring matching over the same canonical lines.
        hits = []
        for path, line_number, text in table:
            column = text.find(literal)
            if column >= 0:
                hits.append(SearchHit(path=path, line=line_number, column=column, text=text))
        result.hits = sorted(hits, key=lambda item: (item.path, item.line, item.column))
        result.backend = "python"
        result.degraded_reason = degraded
        if degraded is not None:
            log.warning("[Context.search] degraded backend reason=%s", degraded)

    if len(result.hits) > effective_limit:
        result.hits = result.hits[:effective_limit]
        result.truncated = True
    log.debug(
        "[Context.search] backend=%s hits=%s truncated=%s lines=%s",
        result.backend,
        len(result.hits),
        result.truncated,
        len(table),
    )
    return result
