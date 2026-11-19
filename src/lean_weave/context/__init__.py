"""Deterministic, stage-scoped context retrieval for lean-weave.

The package is intentionally LLM-free: it scans local snapshots, parses Python
with :mod:`ast`, ranks candidates deterministically, and renders bounded prompt
messages. Nothing here writes workflow state or opens a provider.
"""

from lean_weave.context.models import (
    CodeExcerpt,
    ContextPack,
    ContextQuery,
    DigestEntry,
    ExpansionLevel,
    Omission,
    OmissionReason,
    SourceRange,
    SymbolKind,
    SymbolRef,
    content_digest,
    iter_lines,
    normalize_relative_path,
)

__all__ = [
    "CodeExcerpt",
    "ContextPack",
    "ContextQuery",
    "DigestEntry",
    "ExpansionLevel",
    "Omission",
    "OmissionReason",
    "SourceRange",
    "SymbolKind",
    "SymbolRef",
    "content_digest",
    "iter_lines",
    "normalize_relative_path",
]
