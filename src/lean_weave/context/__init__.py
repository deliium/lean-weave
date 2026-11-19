"""Deterministic, stage-scoped context retrieval for lean-weave.

The package is intentionally LLM-free: it scans local snapshots, parses Python
with :mod:`ast`, ranks candidates deterministically, and renders bounded prompt
messages. Nothing here writes workflow state or opens a provider.
"""

from lean_weave.context.builder import ContextBuilder, BuildResult, build_metadata_context
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
    SymbolKind,
    SymbolRef,
    content_digest,
    iter_lines,
    normalize_relative_path,
)
from lean_weave.context.provider import DeterministicContextProvider
from lean_weave.context.python_index import PythonIndex, build_python_index, parse_module
from lean_weave.context.ranking import rank, tokenize
from lean_weave.context.repository import FileSnapshot, Repository
from lean_weave.context.repository_map import build_repository_map
from lean_weave.context.search import SearchHit, SearchResult, search as literal_search

__all__ = [
    "BuildResult",
    "CodeExcerpt",
    "ContextBuilder",
    "ContextPack",
    "ContextQuery",
    "DeterministicContextProvider",
    "DigestEntry",
    "ExpansionLevel",
    "FileSnapshot",
    "GitMetadata",
    "Omission",
    "OmissionReason",
    "PythonIndex",
    "Repository",
    "SearchHit",
    "SearchResult",
    "SourceRange",
    "SymbolKind",
    "SymbolRef",
    "build_metadata_context",
    "build_python_index",
    "build_repository_map",
    "content_digest",
    "iter_lines",
    "literal_search",
    "normalize_relative_path",
    "parse_module",
    "rank",
    "read_git_metadata",
    "tokenize",
]
