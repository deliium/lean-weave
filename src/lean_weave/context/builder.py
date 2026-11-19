"""Deterministic context pack assembly with progressive expansion.

Whole units are admitted or omitted. Code is never cut to hit a token or
character limit: an over-budget range falls back to its signature, an
over-budget full file falls back to lower detail, and the omission is recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from lean_weave.config.models import ContextConfig
from lean_weave.context.git_metadata import GitMetadata, read_git_metadata
from lean_weave.context.models import (
    CodeExcerpt,
    ContextPack,
    ContextQuery,
    DigestEntry,
    ExpansionLevel,
    Omission,
    OmissionReason,
    SourceRange,
    SymbolRef,
    content_digest,
)
from lean_weave.context.python_index import PythonIndex, build_python_index
from lean_weave.context.ranking import Ranked, rank
from lean_weave.context.render import (
    render_messages,
    render_system,
    render_user,
)
from lean_weave.context.repository import FileSnapshot, Repository
from lean_weave.context.repository_map import build_repository_map
from lean_weave.errors import InvalidContextRequest
from lean_weave.logging_setup import get_logger
from lean_weave.metrics.budget import estimate_tokens

log = get_logger("context.builder")


@dataclass
class BuildResult:
    pack: ContextPack
    messages: list[dict[str, str]]
    system_prompt: str
    user_prompt: str
    file_excerpts: dict[str, str]
    digests: dict[str, str]


@dataclass
class _State:
    map_lines: list[str] = field(default_factory=list)
    symbols: list[SymbolRef] = field(default_factory=list)
    excerpts: list[CodeExcerpt] = field(default_factory=list)
    digests_text: list[str] = field(default_factory=list)
    omissions: list[Omission] = field(default_factory=list)


def slice_lines(snapshot: FileSnapshot, start: int, end: int) -> str:
    lines = snapshot.text.split("\n")
    if snapshot.text.endswith("\n"):
        lines.pop()
    if start < 1 or end < start or end > len(lines):
        raise InvalidContextRequest("Requested line range is outside the file")
    return "\n".join(lines[start - 1 : end])


class ContextBuilder:
    """Build one pack from an explicit query; no state or provider mutation."""

    def __init__(
        self,
        repository: Repository,
        *,
        config: ContextConfig | None = None,
        index: PythonIndex | None = None,
        metadata: GitMetadata | None = None,
    ) -> None:
        self.repository = repository
        self.config = config if config is not None else repository.config
        self.index = index
        self.metadata = metadata

    # ------------------------------------------------------------------
    # Prompt fitting
    # ------------------------------------------------------------------
    def _messages(
        self,
        query: ContextQuery,
        state: _State,
        *,
        include_optional: bool = True,
    ) -> list[dict[str, str]]:
        system = render_system(query.constraints)
        user = render_user(
            task=query.task,
            level=query.level.value,
            stage=query.stage,
            map_lines=state.map_lines if include_optional else [],
            symbols=state.symbols if include_optional else [],
            excerpts=state.excerpts if include_optional else [],
            digests=self._digest_entries(state) if include_optional else [],
            digests_text=state.digests_text,
        )
        return render_messages(system, user)

    def _digest_entries(self, state: _State) -> list[DigestEntry]:
        seen: set[str] = set()
        entries: list[DigestEntry] = []
        for excerpt in state.excerpts:
            if excerpt.path in seen:
                continue
            seen.add(excerpt.path)
            entries.append(
                DigestEntry(path=excerpt.path, digest=excerpt.digest, chars=len(excerpt.text))
            )
        return entries

    def _fits(self, query: ContextQuery, state: _State) -> bool:
        messages = self._messages(query, state)
        return estimate_tokens(messages) <= self.config.stage_prompt_tokens_max

    # ------------------------------------------------------------------
    # Optional units
    # ------------------------------------------------------------------
    def _try_map(self, query: ContextQuery, state: _State, lines: Iterable[str]) -> bool:
        candidate = _State(
            map_lines=sorted(set(lines)),
            symbols=list(state.symbols),
            excerpts=list(state.excerpts),
            digests_text=list(state.digests_text),
            omissions=list(state.omissions),
        )
        if not self._fits(query, candidate):
            state.omissions.append(Omission(reason=OmissionReason.BUDGET, detail="repository_map"))
            return False
        state.map_lines = candidate.map_lines
        return True

    def _try_symbol(self, query: ContextQuery, state: _State, symbol: SymbolRef) -> bool:
        text = symbol.signature or symbol.simple_name
        if not text:
            return False
        if len(text) > self.config.per_file_chars_max:
            state.omissions.append(
                Omission(reason=OmissionReason.BUDGET, path=symbol.path, detail="per_file")
            )
            return False
        key = (symbol.path, symbol.start_line, symbol.end_line, symbol.qualified_name)
        if any(
            (item.path, item.start_line, item.end_line, item.qualified_name) == key
            for item in state.symbols
        ):
            return False
        candidate = [*state.symbols, symbol]
        trial_state = _State(
            map_lines=list(state.map_lines),
            symbols=candidate,
            excerpts=list(state.excerpts),
            digests_text=list(state.digests_text),
            omissions=list(state.omissions),
        )
        if not self._fits(query, trial_state):
            state.omissions.append(Omission(reason=OmissionReason.BUDGET, path=symbol.path))
            return False
        state.symbols = candidate
        return True

    def _try_excerpt(
        self,
        query: ContextQuery,
        state: _State,
        excerpt: CodeExcerpt,
    ) -> bool:
        text = excerpt.text
        if not text:
            return False
        original_excerpts = list(state.excerpts)
        original_symbols = list(state.symbols)
        candidate = list(state.excerpts)
        symbols = list(state.symbols)

        # Identical code: retain one copy and attach alternate provenance.
        for index, existing in enumerate(candidate):
            if existing.text != text:
                continue
            new_range = SourceRange(
                path=excerpt.path, start_line=excerpt.start_line, end_line=excerpt.end_line
            )
            if any(
                item.path == new_range.path
                and item.start_line == new_range.start_line
                and item.end_line == new_range.end_line
                for item in existing.provenance
            ):
                state.omissions.append(
                    Omission(reason=OmissionReason.DUPLICATE, path=excerpt.path)
                )
                return False
            updated = existing.model_copy(
                update={"provenance": [*existing.provenance, new_range]}
            )
            trial = list(candidate)
            trial[index] = updated
            trial_state = _State(
                map_lines=list(state.map_lines),
                symbols=symbols,
                excerpts=trial,
                digests_text=list(state.digests_text),
                omissions=list(state.omissions),
            )
            if self._fits(query, trial_state):
                state.excerpts = trial
                return False
            state.omissions.append(Omission(reason=OmissionReason.BUDGET, path=excerpt.path))
            return False

        # Signatures are metadata fragments, not source bytes: they may share a
        # definition span with other signatures. A body that already contains a
        # signature supersedes it, but signatures never block one another.
        if excerpt.kind == "symbol":
            if any(
                item.path == excerpt.path
                and item.kind in {"range", "full", "partial"}
                and item.start_line <= excerpt.start_line
                and item.end_line >= excerpt.end_line
                for item in candidate
            ):
                state.omissions.append(
                    Omission(reason=OmissionReason.SUPERSEDED, path=excerpt.path)
                )
                return False
        else:
            # Avoid overlapping same-file bytes.
            contained: list[CodeExcerpt] = []
            for existing in list(candidate):
                if existing.path != excerpt.path:
                    continue
                if existing.start_line <= excerpt.start_line and existing.end_line >= excerpt.end_line:
                    state.omissions.append(
                        Omission(
                            reason=(
                                OmissionReason.SUPERSEDED
                                if (
                                    existing.start_line == excerpt.start_line
                                    and existing.end_line == excerpt.end_line
                                )
                                else OmissionReason.OVERLAP
                            ),
                            path=excerpt.path,
                        )
                    )
                    return False
                if (
                    excerpt.start_line <= existing.start_line
                    and excerpt.end_line >= existing.end_line
                ):
                    contained.append(existing)
                elif not (
                    excerpt.end_line < existing.start_line or excerpt.start_line > existing.end_line
                ):
                    state.omissions.append(
                        Omission(reason=OmissionReason.OVERLAP, path=excerpt.path)
                    )
                    return False
            for existing in contained:
                candidate.remove(existing)

        # A body already contains its signature; avoid printing it twice.
        if excerpt.kind in {"range", "full", "partial"}:
            symbols = [
                item
                for item in symbols
                if not (
                    item.path == excerpt.path
                    and item.start_line >= excerpt.start_line
                    and item.end_line <= excerpt.end_line
                )
            ]
        # Character caps, applied to complete units only.
        if len(text) > self.config.per_file_chars_max:
            state.omissions.append(Omission(reason=OmissionReason.BUDGET, path=excerpt.path, detail="per_file"))
            return False
        per_file = sum(len(item.text) for item in candidate if item.path == excerpt.path)
        if per_file + len(text) > self.config.per_file_chars_max:
            state.omissions.append(Omission(reason=OmissionReason.BUDGET, path=excerpt.path, detail="per_file"))
            return False
        total = sum(len(item.text) for item in candidate) + len(text)
        if total > self.config.total_code_chars_max:
            state.omissions.append(Omission(reason=OmissionReason.BUDGET, detail="total_code"))
            return False
        if query.stage == "retry" and total > self.config.retry_excerpt_chars_max:
            state.omissions.append(Omission(reason=OmissionReason.BUDGET, detail="retry_excerpt"))
            return False

        candidate.append(excerpt)
        candidate.sort(key=lambda item: (item.path, item.start_line, item.end_line, item.digest))
        trial_state = _State(
            map_lines=list(state.map_lines),
            symbols=symbols,
            excerpts=candidate,
            digests_text=list(state.digests_text),
            omissions=list(state.omissions),
        )
        if not self._fits(query, trial_state):
            state.excerpts = original_excerpts
            state.symbols = original_symbols
            state.omissions.append(Omission(reason=OmissionReason.BUDGET, path=excerpt.path))
            return False
        state.excerpts = candidate
        state.symbols = symbols
        return True

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------
    def build(
        self,
        query: ContextQuery,
        *,
        metadata_digests: Sequence[tuple[str, str]] = (),
    ) -> BuildResult:
        index = self.index if self.index is not None else build_python_index(self.repository)
        metadata = self.metadata
        if query.allow_git and metadata is None and query.stage is None:
            metadata = read_git_metadata(self.repository)
        ranked = rank(
            self.repository,
            index,
            query,
            metadata=metadata,
            include_tests=query.include_tests,
        )
        scope = set(query.scope_paths)
        if scope:
            ranked.files = [item for item in ranked.files if item.path in scope]
            ranked.symbols = [item for item in ranked.symbols if item.path in scope]

        state = _State(
            omissions=list(self.repository.omissions()),
            digests_text=[],
        )
        for label, text in metadata_digests:
            bounded = (
                text
                if len(text) <= self.config.digest_chars_max
                else text[: self.config.digest_chars_max] + "\n[digest truncated]"
            )
            state.digests_text.append(f"- {label}: {bounded}")
        messages = self._messages(query, state, include_optional=False)
        required_tokens = estimate_tokens(messages)
        if required_tokens > self.config.stage_prompt_tokens_max:
            from lean_weave.errors import ContextBudgetError

            raise ContextBudgetError("Required context envelope exceeds the stage prompt budget")

        # Repository map is the first optional level.
        map_lines, map_omissions = build_repository_map(self.repository, index)
        state.omissions.extend(map_omissions)
        self._try_map(query, state, map_lines)

        if query.line_range is not None:
            snapshot = self.repository.read(query.line_range.path)
            try:
                text = slice_lines(snapshot, query.line_range.start_line, query.line_range.end_line)
            except InvalidContextRequest:
                state.omissions.append(
                    Omission(
                        reason=OmissionReason.INVALID_RANGE,
                        path=query.line_range.path,
                    )
                )
            else:
                self._try_excerpt(
                    query,
                    state,
                    CodeExcerpt(
                        path=snapshot.path,
                        start_line=query.line_range.start_line,
                        end_line=query.line_range.end_line,
                        text=text,
                        digest=snapshot.digest,
                        kind="partial",
                    ),
                )
        elif query.level == ExpansionLevel.FULL:
            path = query.paths[0]
            snapshot = self.repository.read(path)
            full = CodeExcerpt(
                path=path,
                start_line=1,
                end_line=snapshot.lines,
                text=snapshot.text,
                digest=snapshot.digest,
                kind="full",
            )
            admitted = self._try_excerpt(query, state, full)
            if not admitted:
                # Keep lower detail rather than cutting the file.
                for symbol in index.symbols_for(path):
                    self._try_symbol(query, state, symbol)
        elif query.level in {ExpansionLevel.SYMBOLS, ExpansionLevel.RANGES}:
            for symbol in ranked.symbols:
                if query.level == ExpansionLevel.SYMBOLS:
                    self._try_symbol(query, state, symbol)
                    continue
                snapshot = self.repository.try_read(symbol.path)
                if snapshot is None:
                    state.omissions.append(
                        Omission(reason=OmissionReason.NOT_ELIGIBLE, path=symbol.path)
                    )
                    continue
                try:
                    text = slice_lines(snapshot, symbol.start_line, symbol.end_line)
                except InvalidContextRequest:
                    state.omissions.append(
                        Omission(reason=OmissionReason.INVALID_RANGE, path=symbol.path)
                    )
                    continue
                self._try_excerpt(
                    query,
                    state,
                    CodeExcerpt(
                        path=symbol.path,
                        start_line=symbol.start_line,
                        end_line=symbol.end_line,
                        text=text,
                        digest=snapshot.digest,
                        kind="range",
                        symbol=symbol.qualified_name,
                    ),
                )
            # Non-Python explicit paths have no AST units; a complete file is
            # their only meaningful range and is labelled as such.
            for path in query.paths:
                if path.lower().endswith((".py", ".pyi")):
                    continue
                if any(item.path == path for item in state.excerpts):
                    continue
                snapshot = self.repository.try_read(path)
                if snapshot is None:
                    continue
                self._try_excerpt(
                    query,
                    state,
                    CodeExcerpt(
                        path=path,
                        start_line=1,
                        end_line=snapshot.lines,
                        text=snapshot.text,
                        digest=snapshot.digest,
                        kind="range",
                    ),
                )

        messages = self._messages(query, state)
        estimated = estimate_tokens(messages)
        selected_symbols: list[SymbolRef] = []
        seen_symbols: set[tuple] = set()
        for symbol in ranked.symbols:
            key = (symbol.path, symbol.start_line, symbol.end_line, symbol.qualified_name)
            if key in seen_symbols:
                continue
            seen_symbols.add(key)
            selected_symbols.append(symbol)
        relevant: set[str] = {item.path for item in ranked.files}
        relevant.update(item.path for item in state.excerpts)
        digests = self._digest_entries(state)
        pack = ContextPack(
            task=query.task,
            level=query.level,
            stage=query.stage,
            constraints=list(query.constraints),
            relevant_paths=sorted(relevant),
            symbols=selected_symbols,
            excerpts=state.excerpts,
            repository_map=state.map_lines,
            digests=digests,
            omissions=state.omissions,
            estimated_tokens=estimated,
            required_tokens=required_tokens,
        )
        file_excerpts: dict[str, str] = {}
        digest_map: dict[str, str] = {}
        for excerpt in state.excerpts:
            file_excerpts.setdefault(excerpt.path, "")
            if file_excerpts[excerpt.path]:
                file_excerpts[excerpt.path] += "\n"
            file_excerpts[excerpt.path] += excerpt.text
            digest_map.setdefault(excerpt.path, excerpt.digest)
        system = render_system(query.constraints)
        user = messages[1]["content"]
        log.debug(
            "[Context.build] level=%s files=%s symbols=%s excerpts=%s map=%s omitted=%s tokens=%s required=%s",
            query.level.value,
            len(pack.relevant_paths),
            len(pack.symbols),
            len(pack.excerpts),
            len(pack.repository_map),
            len(pack.omissions),
            estimated,
            required_tokens,
        )
        return BuildResult(
            pack=pack,
            messages=messages,
            system_prompt=system,
            user_prompt=user,
            file_excerpts=file_excerpts,
            digests=digest_map,
        )


def build_metadata_context(
    *,
    task: str,
    stage: str,
    constraints: Sequence[str],
    metadata_digests: Sequence[tuple[str, str]] = (),
    level: ExpansionLevel = ExpansionLevel.MAP,
    max_tokens: int = 6000,
    digest_chars_max: int = 2000,
) -> BuildResult:
    """Plan/verify path: no repository discovery, search, Git, or source reads."""

    digests_text: list[str] = []
    digest_entries: list[DigestEntry] = []
    for label, text in metadata_digests:
        bounded = text if len(text) <= digest_chars_max else text[:digest_chars_max] + "\n[digest truncated]"
        digests_text.append(f"- {label}: {bounded}")
        digest_entries.append(
            DigestEntry(path=f"metadata/{label}", digest=content_digest(bounded), chars=len(bounded))
        )
    system = render_system(constraints)
    user = render_user(
        task=task,
        level=level.value,
        stage=stage,
        map_lines=[],
        symbols=[],
        excerpts=[],
        digests=digest_entries,
        digests_text=digests_text,
    )
    messages = render_messages(system, user)
    estimated = estimate_tokens(messages)
    if estimated > max_tokens:
        from lean_weave.errors import ContextBudgetError

        raise ContextBudgetError("Required context envelope exceeds the stage prompt budget")
    pack = ContextPack(
        task=task,
        level=level,
        stage=stage,
        constraints=list(constraints),
        digests=digest_entries,
        estimated_tokens=estimated,
        required_tokens=estimated,
    )
    return BuildResult(
        pack=pack,
        messages=messages,
        system_prompt=system,
        user_prompt=user,
        file_excerpts={},
        digests={entry.path: entry.digest for entry in digest_entries},
    )
