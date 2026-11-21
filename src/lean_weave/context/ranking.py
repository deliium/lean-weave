"""Deterministic lexicographic ranking inside an already-eligible file set.

Signals are evaluated in one documented precedence order. Recency is only a
tie-breaker and can never qualify an otherwise unrelated candidate. No runtime
hash order, walk order, or timestamp is used for ordering.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Iterable

from lean_weave.context.git_metadata import GitMetadata, recency_key
from lean_weave.context.models import ContextQuery, SymbolRef
from lean_weave.context.python_index import PythonIndex
from lean_weave.context.repository import Repository
from lean_weave.errors import ForbiddenContext
from lean_weave.logging_setup import get_logger

log = get_logger("context.ranking")

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
        "add",
        "change",
        "create",
        "fix",
        "implement",
        "make",
        "update",
        "use",
        "using",
    }
)
_STOPWORDS = STOPWORDS


def is_stopword(term: str) -> bool:
    """Return True when *term* is a V1 ranking stopword (case-insensitive)."""

    return term.lower() in STOPWORDS
_NEUTRAL_RECENCY = 1 << 30
_MAX_TERMS = 64


def tokenize(text: str) -> list[str]:
    terms: list[str] = []
    for match in _TOKEN.finditer(text or ""):
        raw = match.group(0)
        parts = re.split(r"_+", raw)
        for part in [raw, *parts]:
            lowered = part.lower()
            if len(lowered) < 2 or lowered in _STOPWORDS or lowered in terms:
                continue
            terms.append(lowered)
            if len(terms) >= _MAX_TERMS:
                return terms
    return terms


@dataclass(frozen=True)
class FileScore:
    path: str
    exact_symbol: int
    explicit_path: int
    term_matches: int
    import_related: int
    path_relevance: int
    recency: int
    symbols: tuple[SymbolRef, ...] = ()

    def key(self) -> tuple:
        return (
            -self.exact_symbol,
            -self.explicit_path,
            -self.term_matches,
            -self.import_related,
            -self.path_relevance,
            self.recency,
            self.path,
        )

    def qualifies(self) -> bool:
        return bool(
            self.exact_symbol
            or self.explicit_path
            or self.term_matches
            or self.import_related
            or self.path_relevance
        )


@dataclass
class Ranked:
    files: list[FileScore] = field(default_factory=list)
    symbols: list[SymbolRef] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    seed_paths: list[str] = field(default_factory=list)
    related_paths: list[str] = field(default_factory=list)
    test_paths: list[str] = field(default_factory=list)


def _symbol_exact(symbol: SymbolRef, hints: set[str]) -> int:
    if not hints:
        return 0
    if symbol.qualified_name in hints:
        return 2
    if symbol.simple_name in hints:
        return 1
    if any(symbol.qualified_name.endswith(f".{hint}") for hint in hints):
        return 2
    return 0


def _term_hits(values: Iterable[str], terms: list[str]) -> int:
    haystack = "\n".join(values).lower()
    return sum(1 for term in terms if term in haystack)


def _is_test_path(path: str) -> bool:
    pure = PurePosixPath(path)
    return (
        "tests" in pure.parts[:-1]
        or pure.name.startswith("test_")
        or pure.name.endswith("_test.py")
    )


def matching_tests(eligible: set[str], sources: Iterable[str]) -> list[str]:
    stems: set[str] = set()
    for source in sources:
        stem = PurePosixPath(source).stem
        if stem and stem != "__init__":
            stems.add(stem)
    matched: set[str] = set()
    for path in eligible:
        pure = PurePosixPath(path)
        if pure.suffix != ".py":
            continue
        parent_parts = pure.parts[:-1]
        if "tests" not in parent_parts and not pure.name.startswith("test_"):
            continue
        for stem in stems:
            if pure.name in {f"test_{stem}.py", f"{stem}_test.py"}:
                matched.add(path)
    return sorted(matched)


def rank(
    repository: Repository,
    index: PythonIndex,
    query: ContextQuery,
    *,
    metadata: GitMetadata | None = None,
    include_tests: bool = False,
) -> Ranked:
    eligible = set(repository.inventory().files)
    for path in query.paths:
        if path not in eligible:
            raise ForbiddenContext("Requested path is not available for context retrieval")

    terms = tokenize(query.task)
    hints = {hint for hint in query.symbols if hint}
    explicit_paths = set(query.paths)
    if not include_tests:
        eligible = {
            path
            for path in eligible
            if path in explicit_paths or not _is_test_path(path)
        }

    # Exact task-term symbol matches are seeds too (unless a selector is exact).
    for symbol in index.symbols:
        if symbol.path not in eligible:
            continue
        if symbol.simple_name.lower() in terms or symbol.qualified_name.lower() in terms:
            if not query.exact_symbols_only:
                hints.add(symbol.simple_name)

    seed_paths = set(explicit_paths)
    for symbol in index.symbols:
        if _symbol_exact(symbol, hints):
            seed_paths.add(symbol.path)

    related: set[str] = set()
    if seed_paths:
        related.update(index.one_hop(seed_paths))
        related.update(
            path
            for path, edges in index.import_edges.items()
            if any(seed in edges for seed in seed_paths)
        )

    test_paths: set[str] = set()
    if include_tests and seed_paths:
        test_paths.update(matching_tests(eligible, seed_paths))
        related.update(test_paths)

    files: list[FileScore] = []
    symbols: list[SymbolRef] = []
    for path in sorted(eligible):
        path_symbols = tuple(index.symbols_for(path))
        exact = max((_symbol_exact(symbol, hints) for symbol in path_symbols), default=0)
        term_matches = _term_hits([path, *(symbol.simple_name for symbol in path_symbols)], terms)
        path_relevance = _term_hits([path], terms)
        score = FileScore(
            path=path,
            exact_symbol=exact,
            explicit_path=1 if path in explicit_paths else 0,
            term_matches=min(term_matches, len(terms)),
            import_related=1 if path in related else 0,
            path_relevance=min(path_relevance, len(terms)),
            recency=recency_key(metadata, path),
            symbols=path_symbols,
        )
        files.append(score)
        if not path_symbols:
            continue
        for symbol in path_symbols:
            symbol_exact = _symbol_exact(symbol, hints)
            symbol_terms = _term_hits(
                [symbol.simple_name, symbol.qualified_name, symbol.parent or ""], terms
            )
            if (
                (symbol_exact or (not query.exact_symbols_only and (
                    symbol_terms
                    or path in explicit_paths
                    or path in related
                    or path_relevance
                )))
            ):
                symbols.append(symbol)

    files.sort(key=lambda item: item.key())
    symbols.sort(
        key=lambda item: (
            -_symbol_exact(item, hints),
            0 if item.path in explicit_paths else 1,
            -_term_hits([item.simple_name, item.qualified_name, item.parent or ""], terms),
            0 if item.path in related else 1,
            -_term_hits([item.path], terms),
            recency_key(metadata, item.path),
            item.path,
            item.start_line,
            item.qualified_name,
        )
    )
    ranked = Ranked(
        files=[item for item in files if item.qualifies()],
        symbols=symbols,
        terms=terms,
        seed_paths=sorted(seed_paths),
        related_paths=sorted(related),
        test_paths=sorted(test_paths),
    )
    log.debug(
        "[Context.rank] files=%s symbols=%s terms=%s seeds=%s related=%s tests=%s",
        len(ranked.files),
        len(ranked.symbols),
        len(ranked.terms),
        len(ranked.seed_paths),
        len(ranked.related_paths),
        len(ranked.test_paths),
    )
    return ranked
