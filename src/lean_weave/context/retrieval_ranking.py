"""Normalized hybrid ranking signals and provenance selection for retrieval."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Iterable, Literal, Mapping, Sequence

from lean_weave.config.models import RetrievalWeights
from lean_weave.context.models import iter_lines
from lean_weave.context.python_index import module_name_for_path
from lean_weave.context.ranking import matching_tests
from lean_weave.context.retrieval_models import (
    DependencyEvidence,
    HitKind,
    SignalName,
    SignalReason,
    TaskKind,
)
from lean_weave.context.search import FileLiteralEvidence, LiteralLineMatch
from lean_weave.logging_setup import get_logger
from lean_weave.metrics.budget import estimate_tokens

log = get_logger("context.retrieval_ranking")

SIGNAL_ORDER: tuple[SignalName, ...] = (
    "exact_symbol",
    "name",
    "lexical",
    "import_dependency",
    "reverse_dependency",
    "git_diff",
    "related_test",
    "map_importance",
)

_TASK_MULTIPLIERS: dict[TaskKind, dict[SignalName, int]] = {
    "feature": {},
    "bugfix": {"lexical": 2, "git_diff": 2},
    "refactor": {"import_dependency": 2, "reverse_dependency": 2},
    "test": {"related_test": 2, "reverse_dependency": 2},
    "diagnose": {"lexical": 2, "related_test": 2},
}

_PATH_COMPONENT = re.compile(r"[^\W\d]\w*", re.UNICODE)
_WINDOW_LEAD = 3
_FILE_HEAD_LINES = 5
_DEF_MAX_LINES = 40


def live_symbol_id(path: str, local_name: str, kind: str, occurrence: int) -> str:
    payload = json.dumps(
        [path, local_name, kind, occurrence],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"live:{digest}"


def effective_weights(base: RetrievalWeights, task_kind: TaskKind) -> dict[SignalName, int]:
    multipliers = _TASK_MULTIPLIERS.get(task_kind, {})
    return {
        name: int(getattr(base, name)) * multipliers.get(name, 1)
        for name in SIGNAL_ORDER
    }


def path_identifier_components(path: str) -> list[str]:
    parts: list[str] = []
    seen: set[str] = set()
    for piece in PurePosixPath(path).parts:
        stem = piece
        if "." in stem and not stem.startswith("."):
            stem = PurePosixPath(stem).stem
        for match in _PATH_COMPONENT.finditer(stem.replace("-", "_")):
            token = match.group(0)
            key = token.casefold()
            if key in seen:
                continue
            seen.add(key)
            parts.append(token)
    return parts


def exact_symbol_score(qualified_name: str, simple_name: str, selectors: Sequence[str]) -> int:
    best = 0
    for selector in selectors:
        if not selector:
            continue
        if qualified_name == selector:
            best = max(best, 1000)
        elif qualified_name.endswith(f".{selector}"):
            best = max(best, 850)
        elif simple_name == selector:
            best = max(best, 700)
    return best


def name_score_for_file(
    path: str,
    primary: Sequence[str],
    *,
    module_name: str,
) -> int:
    pure = PurePosixPath(path)
    if any(selector == path for selector in primary):
        return 1000
    basename = pure.name
    stem = pure.stem
    if any(selector == module_name or selector == basename for selector in primary):
        return 850
    if any(selector == stem for selector in primary):
        return 750
    if not primary:
        return 0
    components = {item.casefold() for item in path_identifier_components(path)}
    matched = sum(1 for term in primary if term.casefold() in components)
    if matched <= 0:
        return 0
    return min(700, 700 * matched // max(1, len(primary)))


def name_score_for_symbol(
    qualified_name: str,
    simple_name: str,
    primary: Sequence[str],
) -> int:
    if any(selector == qualified_name for selector in primary):
        return 1000
    if any(selector == simple_name for selector in primary):
        return 850
    if not primary:
        return 0
    components = {item.casefold() for item in path_identifier_components(qualified_name)}
    matched = sum(1 for term in primary if term.casefold() in components)
    if matched <= 0:
        return 0
    return min(700, 700 * matched // max(1, len(primary)))


def lexical_score(
    *,
    phrase: bool,
    primary_mask: int,
    expanded_mask: int,
    primary_count: int,
    expanded_count: int,
) -> int:
    if phrase:
        return 1000
    if primary_mask == 0 and expanded_mask == 0:
        return 0
    primary_hits = primary_mask.bit_count()
    expanded_hits = expanded_mask.bit_count()
    return min(
        950,
        800 * primary_hits // max(1, primary_count)
        + 150 * expanded_hits // max(1, expanded_count),
    )


def map_importance_score(incoming: int, outgoing: int) -> int:
    return min(1000, 125 * incoming + 50 * outgoing)


def weighted_points(value: int, weight: int) -> int:
    return int(value) * int(weight)


def relevance_score(signals: Mapping[SignalName, int], weights: Mapping[SignalName, int]) -> int:
    total_points = sum(weighted_points(signals.get(name, 0), weights[name]) for name in SIGNAL_ORDER)
    total_weight = sum(weights[name] for name in SIGNAL_ORDER)
    if total_weight <= 0:
        return 0
    return total_points // total_weight


def ranking_reasons(
    signals: Mapping[SignalName, int], weights: Mapping[SignalName, int]
) -> tuple[SignalReason, ...]:
    reasons: list[SignalReason] = []
    for name in SIGNAL_ORDER:
        value = int(signals.get(name, 0))
        weight = int(weights[name])
        points = weighted_points(value, weight)
        if points <= 0:
            continue
        reasons.append(
            SignalReason(
                signal=name,
                value=value,
                weight=weight,
                weighted_points=points,
            )
        )
    return tuple(reasons)


def is_direct_candidate(
    signals: Mapping[SignalName, int], weights: Mapping[SignalName, int]
) -> bool:
    for name in ("exact_symbol", "name", "lexical"):
        if weighted_points(signals.get(name, 0), weights[name]) > 0:
            return True
    return False


def direct_weighted_points(
    signals: Mapping[SignalName, int], weights: Mapping[SignalName, int]
) -> int:
    return sum(
        weighted_points(signals.get(name, 0), weights[name])
        for name in ("exact_symbol", "name", "lexical")
    )


@dataclass
class RankCandidate:
    kind: HitKind
    path: str
    symbol_id: str | None = None
    qualified_name: str | None = None
    definition_start: int | None = None
    definition_end: int | None = None
    source_digest: str = ""
    source_kind: Literal[
        "indexed_definition",
        "live_definition",
        "lexical_window",
        "file_head",
        "empty_file",
    ] = "file_head"
    signals: dict[SignalName, int] = field(
        default_factory=lambda: {name: 0 for name in SIGNAL_ORDER}
    )
    related: list[DependencyEvidence] = field(default_factory=list)
    start_line: int | None = None
    end_line: int | None = None
    estimated_token_cost: int = 0
    _unrounded_points: int = 0
    _anchor: LiteralLineMatch | None = None
    _primary_mask: int = 0
    _expanded_mask: int = 0
    _phrase: bool = False


def select_file_window(
    lines: Sequence[str],
    *,
    empty: bool,
    lexical_positive: bool,
    anchor: LiteralLineMatch | None,
    char_cap: int,
) -> tuple[int | None, int | None, str, str]:
    """Return start, end, source_kind, and window text."""

    if empty:
        return None, None, "empty_file", ""
    if lexical_positive and anchor is not None:
        start = max(1, anchor.line - _WINDOW_LEAD)
        end = min(len(lines), anchor.line + _WINDOW_LEAD)
        kind = "lexical_window"
        anchor_line = anchor.line
    else:
        start = 1
        end = min(len(lines), _FILE_HEAD_LINES)
        kind = "file_head"
        anchor_line = 1
    start, end, text = _clip_window(lines, start, end, char_cap, anchor_line)
    return start, end, kind, text


def select_definition_window(
    lines: Sequence[str],
    *,
    definition_start: int,
    definition_end: int,
    exact_or_name: bool,
    anchor: LiteralLineMatch | None,
    char_cap: int,
) -> tuple[int, int, str]:
    def_start = max(1, definition_start)
    def_end = max(def_start, min(len(lines), definition_end))
    if exact_or_name:
        start = def_start
        end = min(def_end, def_start + _DEF_MAX_LINES - 1)
        anchor_line = def_start
    elif anchor is not None and def_start <= anchor.line <= def_end:
        start = max(def_start, anchor.line - _WINDOW_LEAD)
        end = min(def_end, start + _DEF_MAX_LINES - 1)
        anchor_line = anchor.line
    else:
        start = def_start
        end = min(def_end, def_start + _DEF_MAX_LINES - 1)
        anchor_line = def_start
    start, end, text = _clip_window(lines, start, end, char_cap, anchor_line)
    return start, end, text


def _clip_window(
    lines: Sequence[str],
    start: int,
    end: int,
    char_cap: int,
    anchor_line: int,
) -> tuple[int, int, str]:
    start = max(1, start)
    end = max(start, min(len(lines), end))
    while start <= end:
        text = "\n".join(lines[start - 1 : end])
        if len(text.encode("utf-8")) <= char_cap:
            return start, end, text
        if start == end:
            return start, end, lines[start - 1]
        # Trim farthest boundary from the anchor; trailing on a tie.
        if (end - anchor_line) >= (anchor_line - start):
            end -= 1
        else:
            start += 1
    return anchor_line, anchor_line, lines[anchor_line - 1] if lines else ""


def estimate_window_cost(text: str, *, empty: bool) -> int:
    if empty:
        return 0
    return estimate_tokens(text)


def expand_seed_paths(
    seeds: Sequence[str],
    *,
    outgoing: Mapping[str, Sequence[str]],
    incoming: Mapping[str, Sequence[str]],
    eligible: set[str],
    path_by_id: Mapping[str, str] | None = None,
) -> tuple[list[str], list[str], bool]:
    """Expand one hop from seeds. Returns (out_paths, in_paths, truncated)."""

    out_paths: list[str] = []
    in_paths: list[str] = []
    truncated = False
    seen_out: set[str] = set()
    seen_in: set[str] = set()
    for seed in seeds:
        outs = [path for path in outgoing.get(seed, ()) if path in eligible and path != seed]
        outs = sorted(outs)[:8]
        if len(outgoing.get(seed, ())) > 8:
            truncated = True
        for path in outs:
            if path in seen_out:
                continue
            seen_out.add(path)
            out_paths.append(path)
        inns = [path for path in incoming.get(seed, ()) if path in eligible and path != seed]
        inns = sorted(inns)[:8]
        if len(incoming.get(seed, ())) > 8:
            truncated = True
        for path in inns:
            if path in seen_in:
                continue
            seen_in.add(path)
            in_paths.append(path)
    return out_paths, in_paths, truncated


def bound_matching_tests(eligible: set[str], seeds: Sequence[str]) -> tuple[list[str], bool]:
    matched = matching_tests(eligible, seeds)
    # Cap at 8 per seed stem group overall list of 8*len(seeds) is already bounded by helper;
    # treat overflow of a global 64 as truncated for safety.
    if len(matched) > 64:
        return matched[:64], True
    return matched, False


def sort_key(candidate: RankCandidate, weights: Mapping[SignalName, int]) -> tuple:
    return (
        -candidate._unrounded_points,
        -weighted_points(candidate.signals.get("exact_symbol", 0), weights["exact_symbol"]),
        -weighted_points(candidate.signals.get("name", 0), weights["name"]),
        0 if candidate.kind == "symbol" else 1,
        candidate.estimated_token_cost,
        candidate.path,
        candidate.start_line if candidate.start_line is not None else -1,
        candidate.end_line if candidate.end_line is not None else -1,
        candidate.symbol_id or "",
    )


def windows_overlap(
    a_start: int | None,
    a_end: int | None,
    b_start: int | None,
    b_end: int | None,
) -> bool:
    if a_start is None or a_end is None or b_start is None or b_end is None:
        return False
    return not (a_end < b_start or b_end < a_start)


def dedupe_and_select(
    candidates: Sequence[RankCandidate],
    *,
    kind: Literal["all", "file", "symbol"],
    top_k: int,
) -> tuple[list[RankCandidate], bool]:
    filtered = [
        item
        for item in candidates
        if kind == "all" or item.kind == kind
    ]
    selected: list[RankCandidate] = []
    seen_file: set[str] = set()
    seen_symbol: set[str] = set()
    selected_ranges: list[tuple[str, int | None, int | None, HitKind]] = []
    truncated = False
    for candidate in filtered:
        if candidate.kind == "file":
            if candidate.path in seen_file:
                continue
        else:
            if candidate.symbol_id in seen_symbol:
                continue
        if kind == "all":
            conflict = False
            for path, start, end, other_kind in selected_ranges:
                if path != candidate.path or other_kind == candidate.kind:
                    continue
                if windows_overlap(candidate.start_line, candidate.end_line, start, end):
                    # Skip later file/symbol overlapping an already selected other kind.
                    # Unselected symbols must never suppress a file: we only skip the current row.
                    conflict = True
                    break
                if (
                    candidate.kind == "symbol"
                    and other_kind == "symbol"
                    and candidate.start_line == start
                    and candidate.end_line == end
                ):
                    conflict = True
                    break
            if conflict:
                continue
        if len(selected) >= top_k:
            truncated = True
            break
        selected.append(candidate)
        if candidate.kind == "file":
            seen_file.add(candidate.path)
        else:
            assert candidate.symbol_id is not None
            seen_symbol.add(candidate.symbol_id)
        selected_ranges.append(
            (candidate.path, candidate.start_line, candidate.end_line, candidate.kind)
        )
    else:
        # Check whether one more nonduplicate would survive.
        pass
    if not truncated:
        # Detect whether additional survivors exist beyond top_k.
        survivors = 0
        seen_file = set()
        seen_symbol = set()
        selected_ranges = []
        for candidate in filtered:
            if candidate.kind == "file":
                if candidate.path in seen_file:
                    continue
            else:
                if candidate.symbol_id in seen_symbol:
                    continue
            if kind == "all":
                conflict = False
                for path, start, end, other_kind in selected_ranges:
                    if path != candidate.path or other_kind == candidate.kind:
                        continue
                    if windows_overlap(candidate.start_line, candidate.end_line, start, end):
                        conflict = True
                        break
                    if (
                        candidate.kind == "symbol"
                        and other_kind == "symbol"
                        and candidate.start_line == start
                        and candidate.end_line == end
                    ):
                        conflict = True
                        break
                if conflict:
                    continue
            survivors += 1
            if candidate.kind == "file":
                seen_file.add(candidate.path)
            else:
                assert candidate.symbol_id is not None
                seen_symbol.add(candidate.symbol_id)
            selected_ranges.append(
                (candidate.path, candidate.start_line, candidate.end_line, candidate.kind)
            )
        truncated = survivors > top_k
    return selected[:top_k], truncated
