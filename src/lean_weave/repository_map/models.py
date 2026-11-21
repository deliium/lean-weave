"""Typed repository-map contracts: sources, symbols, and import relations."""

from __future__ import annotations

from dataclasses import dataclass

GRAPH_CONTRACT_VERSION = 1


@dataclass(frozen=True)
class SourceRef:
    """Inclusive 1-based source range; nullable bounds only for file/module refs."""

    path: str
    digest: str
    start_line: int | None
    end_line: int | None


@dataclass(frozen=True)
class ModuleNode:
    """One admitted indexed file, keyed by stable file_id."""

    file_id: str
    path: str
    module_name: str
    digest: str
    size_bytes: int
    line_count: int
    parse_status: str
    module_symbol_id: str


@dataclass(frozen=True)
class MapSymbol:
    """One indexed definition projected for map queries."""

    symbol_id: str
    file_id: str
    qualified_name: str
    simple_name: str
    kind: str
    parent: str | None
    signature: str | None
    source: SourceRef
    occurrence: int


@dataclass(frozen=True)
class ResolutionCandidate:
    """Ordered ambiguous candidate; never selected by sort alone."""

    file_id: str
    path: str
    module_name: str


@dataclass(frozen=True)
class ImportRelation:
    """One import alias occurrence with traceable static resolution."""

    importer_id: str
    source: SourceRef
    statement_ordinal: int
    name_ordinal: int
    spelling: str
    alias: str | None
    status: str
    reason: str | None
    target_id: str | None
    symbol_id: str | None
    candidates: tuple[ResolutionCandidate, ...] = ()


@dataclass(frozen=True)
class DependencyGraph:
    """Immutable static dependency projection for one checked generation."""

    contract_version: int
    checkout_key: str
    policy_key: str
    extractor_key: str
    schema_version: int
    revision: str
    manifest_sha256: str
    nodes: tuple[ModuleNode, ...]
    symbols: tuple[MapSymbol, ...]
    relations: tuple[ImportRelation, ...]
    forward: dict[str, tuple[str, ...]]
    reverse: dict[str, tuple[str, ...]]
    symbol_references: dict[str, tuple[ImportRelation, ...]]
    neighbors: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class MapLimits:
    """Hard caps for compact map rendering (UTF-8 bytes ≈ estimated tokens)."""

    tokens_max: int = 1024
    rows_max: int = 80

    def __post_init__(self) -> None:
        if not isinstance(self.tokens_max, int) or isinstance(self.tokens_max, bool):
            raise ValueError("tokens_max")
        if not isinstance(self.rows_max, int) or isinstance(self.rows_max, bool):
            raise ValueError("rows_max")
        if not 1 <= self.tokens_max <= 8192:
            raise ValueError("tokens_max")
        if not 1 <= self.rows_max <= 500:
            raise ValueError("rows_max")


@dataclass(frozen=True)
class MapResult:
    """Budgeted map view with aligned row provenance."""

    revision: str
    rows: tuple[str, ...]
    row_paths: tuple[str, ...]
    text: str
    estimated_tokens: int
    truncated: bool
