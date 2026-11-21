"""Compact, escaped, budgeted repository-map row rendering."""

from __future__ import annotations

from lean_weave.context.policy import contains_secret
from lean_weave.repository_map.models import MapLimits, MapResult


def escape_map_field(value: str) -> str:
    """Escape control, format, and angle-bracket characters in repository fields."""

    result: list[str] = []
    for char in value:
        code = ord(char)
        if char in "<>":
            result.append(f"\\x{code:02x}")
        elif char in "\n\r\t" or code < 32 or code == 127:
            result.append(f"\\x{code:02x}")
        else:
            result.append(char)
    return "".join(result)


def _safe(value: str) -> str | None:
    if contains_secret(value):
        return None
    return escape_map_field(value)


def module_row(module_name: str, path: str) -> str | None:
    module = _safe(module_name)
    escaped_path = _safe(path)
    if module is None or escaped_path is None:
        return None
    label = module if module else "(unnamed)"
    return f"{label} :: {escaped_path}"


def definition_row(
    path: str,
    start: int,
    end: int,
    qualified: str,
    signature: str | None,
    *,
    kind: str | None = None,
    short: bool = False,
) -> str | None:
    escaped_path = _safe(path)
    name = _safe(qualified)
    if escaped_path is None or name is None:
        return None
    if short or signature is None:
        kind_label = _safe(kind or "symbol")
        if kind_label is None:
            return None
        return f"{escaped_path}:{start}-{end} {name} [{kind_label}]"
    sig = _safe(signature)
    if sig is None:
        return None
    return f"{escaped_path}:{start}-{end} {name} :: {sig}"


def dependency_row(
    *,
    direction: str,
    path: str,
    start: int,
    end: int,
    spelling: str,
    alias: str | None,
    target: str,
) -> str | None:
    escaped_path = _safe(path)
    name = _safe(spelling)
    target_text = _safe(target)
    if escaped_path is None or name is None or target_text is None:
        return None
    alias_part = ""
    if alias is not None:
        escaped_alias = _safe(alias)
        if escaped_alias is None:
            return None
        alias_part = f" [as {escaped_alias}]"
    return f"{direction} {escaped_path}:{start}-{end} {name}{alias_part} -> {target_text}"


def reference_row(
    *,
    kind: str,
    path: str,
    start: int,
    end: int,
    detail: str,
) -> str | None:
    escaped_path = _safe(path)
    body = _safe(detail)
    if escaped_path is None or body is None:
        return None
    return f"{kind} {escaped_path}:{start}-{end} {body}"


def apply_budget(
    candidates: list[tuple[str, str, str | None]],
    *,
    revision: str,
    limits: MapLimits,
    seed_truncated: bool = False,
) -> MapResult:
    """Admit complete rows greedily.

    Each candidate is ``(row, path, short_row_or_none)``. Short form is tried
    before skipping when the long form does not fit.
    """

    admitted: list[str] = []
    paths: list[str] = []
    used = 0
    truncated = seed_truncated

    def fits(text: str) -> bool:
        size = len(text.encode("utf-8")) + 1  # trailing LF
        return len(admitted) < limits.rows_max and used + size <= limits.tokens_max

    for row, path, short in candidates:
        chosen = None
        if fits(row):
            chosen = row
        elif short is not None and fits(short):
            chosen = short
            truncated = True
        else:
            truncated = True
            continue
        admitted.append(chosen)
        paths.append(path)
        used += len(chosen.encode("utf-8")) + 1

    if not admitted and not truncated:
        return MapResult(
            revision=revision,
            rows=(),
            row_paths=(),
            text="",
            estimated_tokens=0,
            truncated=False,
        )

    text = "".join(f"{row}\n" for row in admitted)
    marker = None
    if truncated:
        if fits("..."):
            marker = "...\n"
        elif not admitted and limits.tokens_max >= 1 and limits.rows_max >= 1:
            # Distinguish budget-empty from genuinely empty.
            marker = "."
            text = ""
            used = 0

    if marker == "...\n":
        text = text + marker
        used += len(marker.encode("utf-8"))
    elif marker == ".":
        text = "."
        used = 1
        truncated = True

    return MapResult(
        revision=revision,
        rows=tuple(admitted),
        row_paths=tuple(paths),
        text=text,
        estimated_tokens=len(text.encode("utf-8")),
        truncated=truncated,
    )
