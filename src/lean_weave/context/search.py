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
from typing import Callable, Iterable, Mapping, Sequence

from lean_weave.context.models import iter_lines
from lean_weave.context.repository import Repository, FileSnapshot
from lean_weave.errors import InvalidContextRequest
from lean_weave.logging_setup import get_logger

log = get_logger("context.search")

RG_TIMEOUT_S = 10.0
RG_OUTPUT_LIMIT = 4 * 1024 * 1024
SNAPSHOT_RG_OUTPUT_LIMIT = 1 * 1024 * 1024
SNAPSHOT_RG_INPUT_MAX = 64 * 1024
MAX_QUERY_CHARS = 2000
MAX_FILE_ANCHORS = 8

LiteralMatchCallback = Callable[[str, int, int, int, bool], None]


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


@dataclass(frozen=True)
class LiteralLineMatch:
    path: str
    line: int
    primary_mask: int
    expanded_mask: int
    phrase: bool


@dataclass(frozen=True)
class FileLiteralEvidence:
    path: str
    primary_mask: int
    expanded_mask: int
    phrase: bool
    anchors: tuple[LiteralLineMatch, ...]
    anchors_capped: bool


@dataclass
class SnapshotLiteralResult:
    files: tuple[FileLiteralEvidence, ...] = ()
    backend: str = "python"
    degraded_reason: str | None = None
    line_count: int = 0
    match_line_count: int = 0
    anchors_capped: bool = False


def validate_literal(query: str) -> str:
    if not isinstance(query, str) or not query:
        raise InvalidContextRequest("Search query must be non-empty")
    if "\x00" in query or "\n" in query or "\r" in query:
        raise InvalidContextRequest("Search query must be a single line without NUL bytes")
    if len(query) > MAX_QUERY_CHARS:
        raise InvalidContextRequest("Search query is too long")
    return query


def _snapshots(
    repository: Repository, paths: Iterable[str] | None = None
) -> tuple[list[FileSnapshot], list[str]]:
    selected = None if paths is None else set(paths)
    snapshots: list[FileSnapshot] = []
    skipped: list[str] = []
    for path in repository.inventory().files:
        if selected is not None and path not in selected:
            continue
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


def _popcount(value: int) -> int:
    return value.bit_count()


def _line_masks(
    text: str,
    phrase: str,
    primary: Sequence[str],
    expanded: Sequence[str],
) -> tuple[int, int, bool]:
    primary_mask = 0
    for index, term in enumerate(primary):
        if term and term in text:
            primary_mask |= 1 << index
    expanded_mask = 0
    for index, term in enumerate(expanded):
        if term and term in text:
            expanded_mask |= 1 << index
    phrase_hit = bool(phrase) and phrase in text
    return primary_mask, expanded_mask, phrase_hit


def _snapshot_line_table(
    lines_by_path: Mapping[str, Sequence[str]],
) -> list[tuple[str, int, str]]:
    table: list[tuple[str, int, str]] = []
    for path in sorted(lines_by_path):
        for number, line in enumerate(lines_by_path[path], start=1):
            table.append((path, number, line))
    return table


def _encoded_table_size(table: Sequence[tuple[str, int, str]]) -> int:
    return sum(len(line.encode("utf-8")) + 1 for _, _, line in table)


def _collect_literals(
    phrase: str, primary: Sequence[str], expanded: Sequence[str]
) -> list[str]:
    literals: list[str] = []
    seen: set[str] = set()
    for item in (phrase, *primary, *expanded):
        if not item or item in seen:
            continue
        seen.add(item)
        literals.append(item)
    return literals


def _run_rg_literals(
    literals: Sequence[str], payload: bytes
) -> tuple[list[int], str | None]:
    """Return validated stream line numbers or a degradation reason."""

    executable = shutil.which("rg")
    if executable is None:
        return [], "rg_missing"
    argv = [
        executable,
        "--no-config",
        "--fixed-strings",
        "--line-number",
        "--no-filename",
        "--no-heading",
        "--color=never",
        "--no-mmap",
    ]
    for literal in literals:
        argv.extend(["-e", literal])
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
    except subprocess.TimeoutExpired:
        return [], "rg_timeout"
    except (OSError, subprocess.SubprocessError):
        return [], "rg_error"
    if completed.returncode == 1:
        return [], None
    if completed.returncode != 0:
        return [], "rg_exit"
    if len(completed.stdout) > SNAPSHOT_RG_OUTPUT_LIMIT:
        return [], "rg_overflow"
    numbers: list[int] = []
    previous = 0
    for raw in completed.stdout.splitlines():
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            return [], "rg_malformed"
        number_text, sep, _text = decoded.partition(":")
        if not sep or not number_text.isdigit():
            return [], "rg_malformed"
        number = int(number_text)
        if number <= previous:
            return [], "rg_malformed"
        previous = number
        numbers.append(number)
    return numbers, None


def _python_literal_hits(
    table: Sequence[tuple[str, int, str]],
    phrase: str,
    primary: Sequence[str],
    expanded: Sequence[str],
) -> list[LiteralLineMatch]:
    hits: list[LiteralLineMatch] = []
    for path, line_number, text in table:
        primary_mask, expanded_mask, phrase_hit = _line_masks(
            text, phrase, primary, expanded
        )
        if not (phrase_hit or primary_mask or expanded_mask):
            continue
        hits.append(
            LiteralLineMatch(
                path=path,
                line=line_number,
                primary_mask=primary_mask,
                expanded_mask=expanded_mask,
                phrase=phrase_hit,
            )
        )
    return hits


def _select_anchors(
    matches: Sequence[LiteralLineMatch],
) -> tuple[tuple[LiteralLineMatch, ...], bool]:
    ordered = sorted(
        matches,
        key=lambda item: (
            0 if item.phrase else 1,
            -_popcount(item.primary_mask),
            -_popcount(item.expanded_mask),
            item.line,
        ),
    )
    capped = len(ordered) > MAX_FILE_ANCHORS
    return tuple(ordered[:MAX_FILE_ANCHORS]), capped


def _build_file_evidence(
    matches: Sequence[LiteralLineMatch],
) -> tuple[tuple[FileLiteralEvidence, ...], bool]:
    by_path: dict[str, list[LiteralLineMatch]] = {}
    for match in matches:
        by_path.setdefault(match.path, []).append(match)
    files: list[FileLiteralEvidence] = []
    any_capped = False
    for path in sorted(by_path):
        path_matches = by_path[path]
        primary_mask = 0
        expanded_mask = 0
        phrase = False
        for match in path_matches:
            primary_mask |= match.primary_mask
            expanded_mask |= match.expanded_mask
            phrase = phrase or match.phrase
        anchors, capped = _select_anchors(path_matches)
        any_capped = any_capped or capped
        files.append(
            FileLiteralEvidence(
                path=path,
                primary_mask=primary_mask,
                expanded_mask=expanded_mask,
                phrase=phrase,
                anchors=anchors,
                anchors_capped=capped,
            )
        )
    return tuple(files), any_capped


def search_snapshot_literals(
    lines_by_path: Mapping[str, Sequence[str]],
    phrase: str,
    primary: Sequence[str],
    expanded: Sequence[str],
    *,
    on_match: LiteralMatchCallback | None = None,
) -> SnapshotLiteralResult:
    """Score one prepared line table with a single batched lexical pass.

    Callers supply canonical lines once. Matching never opens files or returns
    source text in the result. ``on_match`` receives each hit exactly once in
    canonical path/line order after the chosen backend is fully validated.
    """

    table = _snapshot_line_table(lines_by_path)
    result = SnapshotLiteralResult(line_count=len(table))
    if not table:
        log.debug(
            "[Context.snapshot_search] empty input terms=%s",
            len(primary) + len(expanded) + (1 if phrase else 0),
        )
        return result

    literals = _collect_literals(phrase, primary, expanded)
    if not literals:
        return result

    encoded_size = _encoded_table_size(table)
    matches: list[LiteralLineMatch] = []
    backend = "python"
    degraded: str | None = None

    use_rg = encoded_size <= SNAPSHOT_RG_INPUT_MAX
    if use_rg:
        payload = _payload(table)
        numbers, reason = _run_rg_literals(literals, payload)
        if reason is not None:
            degraded = reason
            log.warning("[Context.snapshot_search] degraded backend reason=%s", reason)
            matches = _python_literal_hits(table, phrase, primary, expanded)
            backend = "python"
        else:
            validated: list[LiteralLineMatch] = []
            previous = 0
            valid = True
            for number in numbers:
                if number <= previous or number > len(table):
                    valid = False
                    break
                path, line_number, text = table[number - 1]
                primary_mask, expanded_mask, phrase_hit = _line_masks(
                    text, phrase, primary, expanded
                )
                if not (phrase_hit or primary_mask or expanded_mask):
                    valid = False
                    break
                previous = number
                validated.append(
                    LiteralLineMatch(
                        path=path,
                        line=line_number,
                        primary_mask=primary_mask,
                        expanded_mask=expanded_mask,
                        phrase=phrase_hit,
                    )
                )
            if not valid:
                degraded = "rg_malformed"
                log.warning("[Context.snapshot_search] degraded backend reason=%s", degraded)
                matches = _python_literal_hits(table, phrase, primary, expanded)
                backend = "python"
            else:
                matches = validated
                backend = "rg"
    else:
        matches = _python_literal_hits(table, phrase, primary, expanded)
        backend = "python"
        log.debug(
            "[Context.snapshot_search] python selected encoded_bytes=%s",
            encoded_size,
        )

    if on_match is not None:
        for match in matches:
            on_match(
                match.path,
                match.line,
                match.primary_mask,
                match.expanded_mask,
                match.phrase,
            )

    files, anchors_capped = _build_file_evidence(matches)
    result.files = files
    result.backend = backend
    result.degraded_reason = degraded
    result.match_line_count = len(matches)
    result.anchors_capped = anchors_capped
    log.debug(
        "[Context.snapshot_search] backend=%s lines=%s matches=%s files=%s anchors_capped=%s degraded=%s",
        backend,
        len(table),
        len(matches),
        len(files),
        anchors_capped,
        degraded,
    )
    return result


def search(
    repository: Repository,
    query: str,
    *,
    limit: int | None = None,
    paths: Iterable[str] | None = None,
) -> SearchResult:
    literal = validate_literal(query)
    effective_limit = limit if limit is not None else repository.config.search_hits_max
    if effective_limit <= 0:
        raise InvalidContextRequest("Search limit must be positive")
    if effective_limit > repository.config.search_hits_max:
        raise InvalidContextRequest("Search limit cannot exceed context.search_hits_max")

    snapshots, _ = _snapshots(repository, paths)
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
