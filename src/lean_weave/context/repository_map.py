"""Compact structural repository map; no generated prose summaries."""

from __future__ import annotations

from dataclasses import dataclass

from lean_weave.context.models import Omission, OmissionReason
from lean_weave.context.policy import is_python
from lean_weave.context.python_index import PythonIndex
from lean_weave.context.repository import Repository

#: Deterministic ceiling for the rendered map. The full inventory remains
#: available for ranking; only the prompt-facing map is cut.
DEFAULT_MAP_MAX_ENTRIES = 500


@dataclass(frozen=True)
class MapEntry:
    path: str
    module: str | None
    symbols: int
    lines: int


def build_repository_map(
    repository: Repository,
    index: PythonIndex,
    *,
    max_entries: int = DEFAULT_MAP_MAX_ENTRIES,
) -> tuple[list[str], list[Omission]]:
    files = repository.inventory().files
    entries: list[MapEntry] = []
    omissions: list[Omission] = []
    for path in files:
        if is_python(path):
            parsed = index.modules.get(path)
            entries.append(
                MapEntry(
                    path=path,
                    module=parsed.module if parsed is not None else index.module_for(path),
                    symbols=len(parsed.symbols) if parsed is not None else 0,
                    lines=0,
                )
            )
        else:
            entries.append(MapEntry(path=path, module=None, symbols=0, lines=0))
    entries.sort(key=lambda item: item.path)
    if len(entries) > max_entries:
        omissions.append(
            Omission(
                reason=OmissionReason.MAP_LIMIT,
                detail=f"entries={len(entries) - max_entries}",
            )
        )
        entries = entries[:max_entries]
    rendered = []
    for entry in entries:
        if entry.module:
            rendered.append(f"{entry.path} [{entry.module}] symbols={entry.symbols}")
        else:
            rendered.append(entry.path)
    return rendered, omissions
