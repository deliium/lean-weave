"""Shared deterministic helpers for V2 packing tests."""

from __future__ import annotations

from pathlib import Path

from lean_weave.config.models import ContextConfig, PackingConfig
from lean_weave.context.packer import ContextPacker
from lean_weave.context.packing_models import PackRequest
from lean_weave.context.repository import Repository
from lean_weave.context.retriever import ContextRetriever
from lean_weave.repository_map.models import (
    DependencyGraph,
    ImportRelation,
    ModuleNode,
    SourceRef,
)


def packing(**overrides) -> PackingConfig:
    base = {
        "total_tokens": 8000,
        "output_tokens": 500,
        "system_tokens": 1000,
        "task_tokens": 1000,
        "map_tokens": 800,
        "source_tokens": 3000,
        "tool_tokens": 2000,
        "max_expansions": 2,
        "cumulative_source_tokens": 8000,
    }
    base.update(overrides)
    return PackingConfig(**base)


def make_packer(
    project: Path,
    *,
    config: PackingConfig | None = None,
    counter=None,
    load_graph=None,
    model: str = "deepseek-flash",
    thinking: bool = False,
    visited_entries_max: int | None = None,
) -> ContextPacker:
    overrides = {}
    if visited_entries_max is not None:
        overrides["visited_entries_max"] = visited_entries_max
    context = ContextConfig(packing=config or packing(), **overrides)
    repository = Repository(project, context, strict_source=True)
    retriever = ContextRetriever(repository, load_graph=load_graph)
    return ContextPacker(
        repository,
        retriever=retriever,
        token_counter=counter,
        model=model,
        thinking=thinking,
    )


def request(**overrides) -> PackRequest:
    payload = {
        "task_id": "task-1",
        "query": "helper",
        "task_specification": "Change the helper safely.",
        "system_instructions": "Use only supplied source.",
        "level": "l1",
    }
    payload.update(overrides)
    return PackRequest(**payload)


def graph_for(scan, importer: str = "pkg/core.py", target: str = "pkg/other.py") -> DependencyGraph:
    digests = {item.path: item.digest for item in scan.snapshots}
    nodes = (
        ModuleNode(
            file_id="f1",
            path=importer,
            module_name="pkg.core",
            digest=digests[importer],
            size_bytes=1,
            line_count=1,
            parse_status="ok",
            module_symbol_id="s1",
        ),
        ModuleNode(
            file_id="f2",
            path=target,
            module_name="pkg.other",
            digest=digests[target],
            size_bytes=1,
            line_count=1,
            parse_status="ok",
            module_symbol_id="s2",
        ),
    )
    relation = ImportRelation(
        importer_id="f1",
        source=SourceRef(
            path=importer,
            digest=digests[importer],
            start_line=1,
            end_line=1,
        ),
        statement_ordinal=0,
        name_ordinal=0,
        spelling="other",
        alias=None,
        status="resolved",
        reason=None,
        target_id="f2",
        symbol_id=None,
    )
    return DependencyGraph(
        contract_version=1,
        checkout_key="c",
        policy_key="p",
        extractor_key="e",
        schema_version=1,
        revision="r",
        manifest_sha256="m",
        nodes=nodes,
        symbols=(),
        relations=(relation,),
        forward={"f1": ("f2",), "f2": ()},
        reverse={"f1": (), "f2": ("f1",)},
        symbol_references={},
        neighbors={"f1": ("f2",), "f2": ("f1",)},
    )
