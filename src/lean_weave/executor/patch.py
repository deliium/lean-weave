"""Exact single-file unified diff application.

The patch is checked against the current byte revision and applied with no
fuzz and no offset search. The postimage is built completely before any
replacement. A pre-commit failure leaves the original bytes in place. After
``os.replace`` succeeds, a durability error is reported as committed with a
warning rather than as a rejection.

Newline policy: patch transport is LF (a trailing CR on a transport line is
stripped). Logical line text is compared exactly. Unchanged lines keep their
original terminators. Inserted lines use the file's single terminator
convention, or the edit is rejected when that convention is ambiguous.
"""

from __future__ import annotations

import re

from lean_weave.context.policy import contains_secret
from lean_weave.executor.approval import ToolApproval
from lean_weave.executor.models import (
    ApplyPatchRequest,
    ApplyPatchSuccess,
    GUIDANCE,
    ToolFailure,
    failure,
    file_revision,
    parse_model,
)
from lean_weave.executor.workspace import PhysicalLine, Workspace, encode_snapshot
from lean_weave.logging_setup import get_logger

log = get_logger("executor.patch")

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_NO_NL = "\\ No newline at end of file"
_TERMINATOR = {"lf": "\n", "crlf": "\r\n", "cr": "\r"}


def _reject(status: str) -> ToolFailure:
    if status == "conflict":
        log.warning("[Tools.patch] outcome=conflict")
    else:
        log.debug("[Tools.patch] stage=validate outcome=%s", status)
    return failure(status if status in GUIDANCE else "unsupported")


def _header_path(line: str) -> str | None:
    text = line[4:] if line.startswith("--- ") or line.startswith("+++ ") else ""
    if not text:
        return None
    raw = text.split("\t", 1)[0].strip()
    if not raw or raw == "/dev/null" or raw.startswith('"') or "\\" in raw:
        return None
    return raw


def _same_path(header: str, marker: str, path: str) -> bool:
    if header == path:
        return True
    prefix = f"{marker}/"
    return header.startswith(prefix) and header[len(prefix):] == path


def apply_patch(approval: ToolApproval, request: ApplyPatchRequest | dict) -> ApplyPatchSuccess | ToolFailure:
    parsed = parse_model(ApplyPatchRequest, request)
    if isinstance(parsed, ToolFailure):
        return parsed
    encoded = parsed.diff.encode("utf-8")
    if len(encoded) > approval.limits.patch_bytes_max:
        return _reject("oversized")
    workspace = Workspace(approval)
    workspace.begin()
    loaded = workspace.read(parsed.path)
    if isinstance(loaded, str):
        return _reject(loaded if loaded in GUIDANCE else _map(loaded))
    if loaded.revision != parsed.expected_revision:
        log.warning("[Tools.patch] outcome=stale")
        return failure("stale")
    applied = _apply(loaded.lines, loaded.newline, parsed.diff, parsed.path, approval.limits.patch_hunks_max)
    if isinstance(applied, str):
        return _reject(applied)
    new_lines, added, removed = applied
    try:
        payload = encode_snapshot(loaded.encoding, loaded.bom, new_lines)
    except UnicodeEncodeError:
        return _reject("unsupported")
    if len(payload) > approval.limits.file_bytes_max:
        return _reject("oversized")
    text = "".join(line.content + line.terminator for line in new_lines)
    if contains_secret(text):
        log.warning("[Tools.patch] outcome=secret")
        return failure("secret")
    outcome = workspace.replace_file(loaded, payload)
    if outcome == "confirmed":
        log.info("[Tools.patch] committed lines_added=%s lines_removed=%s", added, removed)
        return ApplyPatchSuccess(
            revision=file_revision(payload),
            lines_added=added,
            lines_removed=removed,
            durability="confirmed",
            guidance=GUIDANCE["committed"],
        )
    if outcome == "unconfirmed":
        log.warning("[Tools.patch] outcome=durability lines_added=%s lines_removed=%s", added, removed)
        return ApplyPatchSuccess(
            revision=file_revision(payload),
            lines_added=added,
            lines_removed=removed,
            durability="unconfirmed",
            guidance=GUIDANCE["committed_unconfirmed"],
        )
    log.debug("[Tools.patch] stage=commit outcome=%s", outcome)
    return _reject(_map(outcome))


def _map(code: str) -> str:
    if code in {"hard_denied", "excluded", "not_in_scope", "denied", "symlink", "invalid"}:
        return "denied"
    if code in {"binary", "undecodable", "unsupported"}:
        return "unsupported"
    if code in {"stale", "conflict", "secret", "identity", "hard_link", "missing", "oversized", "indeterminate"}:
        return code
    if code == "scan_limit":
        return "oversized"
    return "unreadable"


def _apply(
    lines: tuple[PhysicalLine, ...],
    newline: str,
    diff: str,
    path: str,
    hunk_limit: int,
) -> tuple[list[PhysicalLine], int, int] | str:
    body = diff.replace("\r\n", "\n").replace("\r", "\n")
    raw_lines = body.split("\n")
    if body.endswith("\n"):
        raw_lines = raw_lines[:-1]
    index = 0
    if index < len(raw_lines) and raw_lines[index].startswith("diff --git "):
        git_line = raw_lines[index]
        if not _git_header_ok(git_line, path):
            return "unsupported"
        index += 1
    if index + 1 >= len(raw_lines):
        return "unsupported"
    old_header = _header_path(raw_lines[index])
    new_header = _header_path(raw_lines[index + 1])
    if old_header is None or new_header is None:
        return "unsupported"
    if not raw_lines[index].startswith("--- ") or not raw_lines[index + 1].startswith("+++ "):
        return "unsupported"
    if not _same_path(old_header, "a", path) or not _same_path(new_header, "b", path):
        return "unsupported"
    index += 2
    hunks: list[_Hunk] = []
    while index < len(raw_lines):
        if not raw_lines[index]:
            return "unsupported"
        if raw_lines[index].startswith(("index ", "new file", "deleted file", "rename ", "copy ", "old mode", "new mode", "Binary ", "GIT binary")):
            return "unsupported"
        match = _HUNK.match(raw_lines[index])
        if match is None:
            return "unsupported"
        old_start = int(match.group(1))
        old_count = int(match.group(2) if match.group(2) is not None else "1")
        new_count = int(match.group(4) if match.group(4) is not None else "1")
        index += 1
        records: list[tuple[str, str, bool]] = []
        seen_old = 0
        seen_new = 0
        while index < len(raw_lines) and not raw_lines[index].startswith("@@"):
            row = raw_lines[index]
            index += 1
            if row == _NO_NL:
                if not records:
                    return "unsupported"
                kind, text, _marked = records[-1]
                records[-1] = (kind, text, True)
                continue
            if not row or row[0] not in " +-":
                return "unsupported"
            kind = row[0]
            text = row[1:]
            records.append((kind, text, False))
            if kind in " -":
                seen_old += 1
            if kind in " +":
                seen_new += 1
        if seen_old != old_count or seen_new != new_count:
            return "unsupported"
        hunks.append(_Hunk(old_start, old_count, records))
        if len(hunks) > hunk_limit:
            return "oversized"
    if not hunks:
        return "unsupported"
    return _render(lines, newline, hunks)


class _Hunk:
    def __init__(self, old_start: int, old_count: int, records: list[tuple[str, str, bool]]) -> None:
        self.old_start = old_start
        self.old_count = old_count
        self.records = records


def _git_header_ok(line: str, path: str) -> bool:
    parts = line.split()
    if len(parts) != 4 or parts[0] != "diff" or parts[1] != "--git":
        return False
    return _same_path(parts[2], "a", path) and _same_path(parts[3], "b", path)


def _render(
    lines: tuple[PhysicalLine, ...],
    newline: str,
    hunks: list[_Hunk],
) -> tuple[list[PhysicalLine], int, int] | str:
    previous_end = 0
    for hunk in hunks:
        start = hunk.old_start
        if hunk.old_count == 0:
            if start < previous_end:
                return "unsupported"
            previous_end = start
        else:
            if start < 1 or start <= previous_end:
                return "unsupported"
            end = start + hunk.old_count - 1
            if end > len(lines):
                return "conflict"
            previous_end = end
    added = 0
    removed = 0
    has_insert = any(kind == "+" for hunk in hunks for kind, _text, _mark in hunk.records)
    if has_insert and newline in {"mixed", "none"} and len(lines) > 1:
        return "unsupported"
    if has_insert and newline == "mixed":
        return "unsupported"
    convention = _TERMINATOR.get(newline, "\n" if newline == "none" else "")
    if has_insert and newline == "none":
        convention = "\n"
    output: list[PhysicalLine] = []
    cursor = 0
    for hunk in hunks:
        copy_until = hunk.old_start if hunk.old_count == 0 else hunk.old_start - 1
        if copy_until < cursor or copy_until > len(lines):
            return "conflict"
        while cursor < copy_until:
            output.append(lines[cursor])
            cursor += 1
        for kind, text, marked in hunk.records:
            if kind == " ":
                if cursor >= len(lines) or lines[cursor].content != text:
                    return "conflict"
                if cursor == len(lines) - 1 and lines[cursor].terminator == "" and not marked:
                    log.debug("[FIX] patch missing no-newline marker on old final line")
                    return "unsupported"
                if marked and (cursor != len(lines) - 1 or lines[cursor].terminator != ""):
                    return "unsupported"
                output.append(lines[cursor])
                cursor += 1
            elif kind == "-":
                if cursor >= len(lines) or lines[cursor].content != text:
                    return "conflict"
                if cursor == len(lines) - 1 and lines[cursor].terminator == "" and not marked:
                    log.debug("[FIX] patch missing no-newline marker on removed final line")
                    return "unsupported"
                if marked and (cursor != len(lines) - 1 or lines[cursor].terminator != ""):
                    return "unsupported"
                cursor += 1
                removed += 1
            elif kind == "+":
                if marked:
                    output.append(PhysicalLine(text, ""))
                else:
                    if not convention and newline == "mixed":
                        return "unsupported"
                    output.append(PhysicalLine(text, convention or "\n"))
                added += 1
            else:
                return "unsupported"
    while cursor < len(lines):
        output.append(lines[cursor])
        cursor += 1
    if any(marked for hunk in hunks for kind, _text, marked in hunk.records if kind in "+ ") and output:
        # A new-side marker is valid only on the final produced line.
        last_marked = False
        produced = False
        for hunk in hunks:
            for kind, _text, marked in hunk.records:
                if kind in "+ ":
                    produced = True
                    last_marked = marked
                    if marked and not _is_last_new_record(hunks, hunk, kind):
                        return "unsupported"
        if last_marked and output[-1].terminator != "":
            return "unsupported"
        _ = produced
    # Only the final line may omit its terminator. An insertion after a final
    # line without a newline would otherwise concatenate two logical lines.
    if any(line.terminator == "" for line in output[:-1]):
        log.warning("[FIX] patch newline ambiguity at end of file")
        return "unsupported"
    return output, added, removed


def _is_last_new_record(hunks: list[_Hunk], current: _Hunk, kind: str) -> bool:
    seen = False
    last = True
    for hunk in hunks:
        for record_kind, _text, marked in hunk.records:
            if hunk is current and record_kind == kind and marked and not seen:
                seen = True
                continue
            if seen and record_kind in "+ ":
                last = False
    return last
