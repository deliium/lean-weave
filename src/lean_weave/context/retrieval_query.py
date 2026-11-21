"""Deterministic query parsing and bounded term expansion for offline retrieval."""

from __future__ import annotations

import re
from dataclasses import dataclass

from lean_weave.context.ranking import is_stopword
from lean_weave.context.retrieval_models import RetrievalRequest
from lean_weave.logging_setup import get_logger

log = get_logger("context.retrieval_query")

_MAX_PRIMARY = 12
_MAX_EXPANDED = 12
_ID = r"[^\W\d]\w*"
# Identifier runs may include path separators only inside the token.
_TOKEN_RUN = re.compile(rf"{_ID}(?:[./-]{_ID})*")
_CAMEL_SPLIT = re.compile(
    r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+",
    re.UNICODE,
)


@dataclass(frozen=True)
class PreparedQuery:
    phrase: str
    selectors: tuple[str, ...]
    primary: tuple[str, ...]
    expanded: tuple[str, ...]
    primary_capped: bool
    expanded_capped: bool


def _extract_selectors(phrase: str) -> tuple[tuple[str, ...], bool]:
    """Extract up to 12 first-seen identifier / dotted / path-shaped tokens."""

    selectors: list[str] = []
    seen: set[str] = set()
    capped = False
    for match in _TOKEN_RUN.finditer(phrase):
        token = match.group(0)
        if token in seen:
            continue
        if len(selectors) >= _MAX_PRIMARY:
            capped = True
            continue
        seen.add(token)
        selectors.append(token)
    return tuple(selectors), capped


def _primary_terms(selectors: tuple[str, ...]) -> tuple[str, ...]:
    terms: list[str] = []
    seen_lower: set[str] = set()
    for token in selectors:
        if is_stopword(token):
            continue
        key = token.casefold()
        if key in seen_lower:
            continue
        seen_lower.add(key)
        terms.append(token)
    return tuple(terms)


def _component_parts(token: str) -> list[str]:
    parts: list[str] = []
    for chunk in re.split(r"[./\-]+", token):
        if not chunk:
            continue
        if "_" in chunk:
            parts.extend(piece for piece in chunk.split("_") if piece)
            continue
        camel = _CAMEL_SPLIT.findall(chunk)
        if len(camel) > 1 or (camel and camel[0] != chunk):
            parts.extend(part for part in camel if part)
        else:
            parts.append(chunk)
    return parts


def _expanded_terms(selectors: tuple[str, ...]) -> tuple[tuple[str, ...], bool]:
    expanded: list[str] = []
    seen_lower: set[str] = set()
    capped = False
    for token in selectors:
        for part in _component_parts(token):
            if len(part) < 2 or is_stopword(part):
                continue
            key = part.casefold()
            if key in seen_lower:
                continue
            # Also skip parts identical (casefold) to a selector already kept as primary intent.
            if len(expanded) >= _MAX_EXPANDED:
                capped = True
                continue
            seen_lower.add(key)
            expanded.append(part)
    return tuple(expanded), capped


def prepare_query(request: RetrievalRequest) -> PreparedQuery:
    """Return the phrase, exact selectors, and ordered primary/expanded terms."""

    phrase = request.query
    selectors, primary_capped = _extract_selectors(phrase)
    primary = _primary_terms(selectors)
    expanded, expanded_capped = _expanded_terms(selectors)
    log.debug(
        "[Context.retrieval_query] selectors=%s primary=%s expanded=%s primary_capped=%s expanded_capped=%s",
        len(selectors),
        len(primary),
        len(expanded),
        primary_capped,
        expanded_capped,
    )
    return PreparedQuery(
        phrase=phrase,
        selectors=selectors,
        primary=primary,
        expanded=expanded,
        primary_capped=primary_capped,
        expanded_capped=expanded_capped,
    )
