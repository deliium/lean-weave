"""Targeted reads and Python symbol search.

Symbol spans come from the AST index and include decorators. Search returns
locations, not bodies. An oversized complete unit is rejected with its line
span so the caller can request a narrower range; the body is never cut.
"""

from __future__ import annotations

from lean_weave.context.models import content_digest
from lean_weave.context.policy import is_python
from lean_weave.context.python_index import parse_module
from lean_weave.context.repository import FileSnapshot
from lean_weave.executor.approval import ToolApproval
from lean_weave.executor.models import (
    GUIDANCE,
    ReadRangeRequest,
    ReadRangeSuccess,
    ReadSymbolRequest,
    ReadSymbolSuccess,
    SearchSymbolsRequest,
    SearchSymbolsSuccess,
    SymbolLocation,
    ToolFailure,
    cap_result_items,
    failure,
    parse_model,
)
from lean_weave.executor.workspace import TextSnapshot, Workspace
from lean_weave.logging_setup import get_logger

log = get_logger("executor.source")

_DENY = {
    "denied",
    "hard_denied",
    "excluded",
    "not_in_scope",
    "symlink",
    "protected",
    "invalid",
}


def _status(code: str) -> str:
    if code in _DENY:
        return "denied"
    if code in {"binary", "undecodable", "unsupported"}:
        return "unsupported"
    if code in {"oversized", "scan_limit"}:
        return "oversized" if code == "oversized" else "incomplete"
    if code in {"secret", "missing", "indeterminate", "unreadable", "identity", "parse_error"}:
        return code
    return "unreadable"


def _fail(code: str, **kwargs) -> ToolFailure:
    status = _status(code)
    if status not in GUIDANCE:
        status = "unreadable"
    return failure(status, **kwargs)


def _slice(snapshot: TextSnapshot, start: int, end: int) -> str:
    selected = snapshot.lines[start - 1:end]
    return "".join(line.content + line.terminator for line in selected)


def _too_wide(workspace: Workspace, text: str, start: int, end: int) -> ToolFailure | None:
    limits = workspace.approval.limits
    if end - start + 1 > limits.range_lines_max or len(text) > limits.result_chars_max:
        return failure("oversized", start_line=start, end_line=end, limit=limits.range_lines_max)
    return None


def _success_range(snapshot: TextSnapshot, start: int, end: int, text: str) -> ReadRangeSuccess:
    return ReadRangeSuccess(
        path=snapshot.path,
        start_line=start,
        end_line=end,
        text=text,
        revision=snapshot.revision,
        encoding=snapshot.encoding,
        newline=snapshot.newline,
    )


def read_range(approval: ToolApproval, request: ReadRangeRequest | dict) -> ReadRangeSuccess | ToolFailure:
    parsed = parse_model(ReadRangeRequest, request)
    if isinstance(parsed, ToolFailure):
        return parsed
    workspace = Workspace(approval)
    workspace.begin()
    loaded = workspace.read(parsed.path)
    if isinstance(loaded, str):
        log.debug("[Tools.read] operation=read_range outcome=%s", _status(loaded))
        return _fail(loaded)
    count = len(loaded.lines)
    if parsed.start_line > count or parsed.end_line > count:
        log.debug("[Tools.read] operation=read_range outcome=invalid lines=%s", count)
        return failure("invalid")
    text = _slice(loaded, parsed.start_line, parsed.end_line)
    wide = _too_wide(workspace, text, parsed.start_line, parsed.end_line)
    if wide is not None:
        log.debug("[Tools.read] operation=read_range outcome=oversized")
        return wide
    log.debug(
        "[Tools.read] operation=read_range outcome=ok lines=%s",
        parsed.end_line - parsed.start_line + 1,
    )
    return _success_range(loaded, parsed.start_line, parsed.end_line, text)


def _symbols(snapshot: TextSnapshot):
    normalized = "\n".join(line.content for line in snapshot.lines)
    if snapshot.ends_with_newline:
        normalized += "\n"
    parsed = parse_module(
        FileSnapshot(snapshot.path, normalized, content_digest(normalized), len(snapshot.data))
    )
    return parsed


def read_symbol(approval: ToolApproval, request: ReadSymbolRequest | dict) -> ReadSymbolSuccess | ToolFailure:
    parsed = parse_model(ReadSymbolRequest, request)
    if isinstance(parsed, ToolFailure):
        return parsed
    workspace = Workspace(approval)
    workspace.begin()
    loaded = workspace.read(parsed.path)
    if isinstance(loaded, str):
        log.debug("[Tools.read] operation=read_symbol outcome=%s", _status(loaded))
        return _fail(loaded)
    if not is_python(loaded.path):
        log.debug("[Tools.read] operation=read_symbol outcome=unsupported")
        return failure("unsupported")
    try:
        module = _symbols(loaded)
    except (MemoryError, RecursionError):
        log.debug("[Tools.read] operation=read_symbol outcome=parse_error")
        return failure("parse_error")
    if module.error:
        log.debug("[Tools.read] operation=read_symbol outcome=parse_error")
        return failure("parse_error")
    qualified = [
        symbol
        for symbol in module.symbols
        if symbol.qualified_name == parsed.name or symbol.qualified_name.endswith(f".{parsed.name}")
    ]
    if len(qualified) == 1:
        chosen = qualified
    elif len(qualified) > 1:
        chosen = qualified
    else:
        chosen = [symbol for symbol in module.symbols if symbol.simple_name == parsed.name]
    if not chosen:
        log.debug("[Tools.read] operation=read_symbol outcome=missing")
        return failure("missing")
    if len(chosen) > 1:
        limit = workspace.approval.limits.search_hits_max
        candidates = [
            SymbolLocation(
                path=symbol.path,
                qualified_name=symbol.qualified_name,
                kind=symbol.kind.value,
                start_line=symbol.start_line,
                end_line=symbol.end_line,
            )
            for symbol in sorted(chosen, key=lambda item: (item.start_line, item.qualified_name))
        ]
        log.debug("[Tools.read] operation=read_symbol outcome=ambiguous count=%s", len(candidates))
        ambiguous = failure("ambiguous", candidates=candidates[:limit], truncated=len(candidates) > limit)
        return cap_result_items(ambiguous, "candidates", workspace.approval.limits.result_chars_max)
    symbol = chosen[0]
    if symbol.end_line > len(loaded.lines):
        log.debug("[Tools.read] operation=read_symbol outcome=parse_error")
        return failure("parse_error")
    text = _slice(loaded, symbol.start_line, symbol.end_line)
    wide = _too_wide(workspace, text, symbol.start_line, symbol.end_line)
    if wide is not None:
        log.debug("[Tools.read] operation=read_symbol outcome=oversized")
        return wide
    log.debug("[Tools.read] operation=read_symbol outcome=ok lines=%s", symbol.end_line - symbol.start_line + 1)
    return ReadSymbolSuccess(
        path=loaded.path,
        qualified_name=symbol.qualified_name,
        kind=symbol.kind.value,
        start_line=symbol.start_line,
        end_line=symbol.end_line,
        text=text,
        revision=loaded.revision,
        encoding=loaded.encoding,
        newline=loaded.newline,
    )


def _matches(symbol, query: str) -> bool:
    final = symbol.qualified_name.rsplit(".", 1)[-1]
    if symbol.qualified_name == query or final == query or symbol.simple_name == query:
        return True
    if "." in query:
        return symbol.qualified_name.startswith(query)
    return symbol.simple_name.startswith(query) or final.startswith(query)


def search_symbols(
    approval: ToolApproval, request: SearchSymbolsRequest | dict
) -> SearchSymbolsSuccess | ToolFailure:
    parsed = parse_model(SearchSymbolsRequest, request)
    if isinstance(parsed, ToolFailure):
        return parsed
    workspace = Workspace(approval)
    workspace.begin()
    omissions: list[str] = []
    snapshots: dict[str, TextSnapshot] = {}
    complete = True
    if parsed.paths:
        for path in parsed.paths:
            loaded = workspace.read(path)
            if isinstance(loaded, str):
                log.debug("[Tools.search] operation=search_symbols outcome=%s", _status(loaded))
                return _fail(loaded)
            snapshots[loaded.path] = loaded
        paths = list(snapshots)
    else:
        discovery = workspace.discover()
        paths = [path for path in discovery.files if is_python(path)]
        omissions.extend(discovery.omissions)
        complete = discovery.complete
    hits: list[SymbolLocation] = []
    for path in paths:
        loaded = snapshots.get(path)
        if loaded is None:
            loaded = workspace.read(path)
            if isinstance(loaded, str):
                mapped = "scan_limit" if loaded == "scan_limit" else _status(loaded)
                omissions.append(mapped)
                # A candidate that could not be read was not inspected. Returning
                # complete=True here would turn a partial scan into a definitive
                # zero-hit result.
                complete = False
                log.debug("[FIX] search candidate not inspected code=%s complete=False", loaded)
                if loaded == "scan_limit":
                    break
                continue
        if parsed.paths and not is_python(loaded.path):
            log.debug("[Tools.search] operation=search_symbols outcome=unsupported")
            return failure("unsupported")
        try:
            module = _symbols(loaded)
        except (MemoryError, RecursionError):
            omissions.append("parse_error")
            continue
        if module.error:
            omissions.append("parse_error")
            continue
        for symbol in module.symbols:
            if not _matches(symbol, parsed.query):
                continue
            hits.append(
                SymbolLocation(
                    path=symbol.path,
                    qualified_name=symbol.qualified_name,
                    kind=symbol.kind.value,
                    start_line=symbol.start_line,
                    end_line=symbol.end_line,
                )
            )
    limit = min(
        parsed.hit_limit or workspace.approval.limits.search_hits_max,
        workspace.approval.limits.search_hits_max,
    )
    ordered = sorted(hits, key=lambda hit: _rank_location(hit, parsed.query))
    truncated = len(ordered) > limit
    visible = ordered[:limit]
    reasons = list(dict.fromkeys(omissions))
    if workspace._budget.incomplete:
        complete = False
    log.debug(
        "[Tools.search] operation=search_symbols candidates=%s hits=%s complete=%s truncated=%s outcome=%s",
        len(paths),
        len(visible),
        complete,
        truncated,
        "ok" if complete else "incomplete",
    )
    if not complete:
        incomplete = failure(
            "incomplete",
            hits=visible,
            complete=False,
            truncated=truncated,
            omissions=reasons,
        )
        return cap_result_items(incomplete, "hits", workspace.approval.limits.result_chars_max)
    found = SearchSymbolsSuccess(
        hits=visible,
        complete=True,
        truncated=truncated,
        omissions=reasons,
    )
    return cap_result_items(found, "hits", workspace.approval.limits.result_chars_max)


def _rank_location(hit: SymbolLocation, query: str) -> tuple[int, str, int, str]:
    final = hit.qualified_name.rsplit(".", 1)[-1]
    exact = hit.qualified_name == query or final == query
    return (0 if exact else 1, hit.path, hit.start_line, hit.qualified_name)
