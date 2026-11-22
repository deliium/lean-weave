"""Offline context preview: search, build, and inspect without credentials."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import typer
from click.core import ParameterSource
from pydantic import ValidationError

from lean_weave.app import (
    create_context_packer,
    create_indexed_map_preview_pair,
    create_retrieval_provider,
)
from lean_weave.cli.runtime import command_context
from lean_weave.context.builder import ContextBuilder
from lean_weave.context.models import (
    ContextQuery,
    ExpansionLevel,
    SourceRange,
    normalize_relative_path,
)
from lean_weave.context.packing_models import (
    PackEnvelope,
    PackRequest,
    PackResult,
)
from lean_weave.context.policy import contains_secret, is_python
from lean_weave.context.python_index import build_python_index
from lean_weave.context.repository import Repository
from lean_weave.context.retrieval_models import RetrievalRequest, RetrievalResult
from lean_weave.context.search import SearchHit, search as literal_search
from lean_weave.errors import ForbiddenContext, InvalidContextRequest
from lean_weave.logging_setup import get_logger

log = get_logger("cli")

context_app = typer.Typer(
    help="Search and build deterministic, budgeted context packs (offline).",
    no_args_is_help=True,
)


def _escape(value: str) -> str:
    result: list[str] = []
    for char in value:
        code = ord(char)
        if char in "\n\t" or 32 <= code < 127:
            result.append(char)
        elif code < 32 or code == 127:
            result.append(f"\\x{code:02x}")
        else:
            result.append(char)
    return "".join(result)


def _repository(context) -> Repository:
    return Repository(
        Path.cwd(),
        context.config.context,
        state_dir=context.config.workflow.state_dir,
    )


def _guard(value: str, *, what: str) -> str:
    if contains_secret(value):
        raise ForbiddenContext(f"{what} was rejected by the credential filter")
    return value


def _path(value: str) -> str:
    try:
        normalized = normalize_relative_path(value)
    except InvalidContextRequest as exc:
        raise typer.BadParameter(str(exc)) from None
    _guard(normalized, what="Context path")
    return normalized


def _emit(payload: dict, *, json_output: bool, human: str) -> None:
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        typer.echo(_escape(human).rstrip("\n"))


_PREVIEW_SYSTEM = (
    "You are a read-only coding assistant. Use only the supplied source and "
    "repository map. Do not assume omitted code, and do not modify files."
)


def _explicit(ctx: typer.Context, name: str) -> bool:
    source = ctx.get_parameter_source(name)
    return source is ParameterSource.COMMANDLINE


def _resolve_packed_mode(
    ctx: typer.Context,
    *,
    packed: bool,
    legacy: bool,
    pack_level: str,
    legacy_options: tuple[str, ...],
) -> bool:
    packed_signal = packed or _explicit(ctx, "pack_level")
    legacy_signal = legacy or any(_explicit(ctx, name) for name in legacy_options)
    if packed_signal and legacy_signal:
        raise typer.BadParameter("Packed and legacy context options cannot be combined")
    if packed_signal:
        return True
    if legacy_signal:
        return False
    return command_context(ctx).config.context.packing.cli_default


def _pack_level(value: str) -> str:
    if value not in {"l0", "l1"}:
        raise typer.BadParameter("--pack-level must be l0 or l1")
    return value


def _task_id(query: str) -> str:
    return "cli:" + hashlib.sha256(query.encode("utf-8")).hexdigest()[:24]


def _run_packed(
    ctx: typer.Context,
    *,
    query: str,
    level: str,
) -> PackResult:
    context = command_context(ctx)
    _guard(query, what="Task query")
    try:
        request = PackRequest(
            task_id=_task_id(query),
            query=query,
            task_specification=query,
            system_instructions=_PREVIEW_SYSTEM,
            level=_pack_level(level),  # type: ignore[arg-type]
            envelope=PackEnvelope(
                model=context.config.llm.model,
                thinking="enabled" if context.config.llm.thinking else "disabled",
            ),
        )
    except (ValidationError, ForbiddenContext) as exc:
        raise typer.BadParameter("Invalid packed context request") from exc
    packer = create_context_packer(context)
    return packer.build(request)


def _composition_payload(result: PackResult) -> dict:
    pack = result.pack
    if pack is None:
        return {
            "status": result.status,
            "reason_codes": list(result.reason_codes),
            "omissions": [],
        }
    return {
        "version": pack.version,
        "level": pack.level,
        "status": pack.status,
        "reason_codes": list(pack.reason_codes),
        "omissions": [item.model_dump(mode="json") for item in pack.omissions],
        "composition": pack.metrics.model_dump(mode="json"),
        "scan_incomplete": pack.scan_incomplete,
        "degraded_reasons": list(pack.degraded_reasons),
    }


def _packed_payload(
    result: PackResult, *, command: Literal["build", "inspect", "budget"]
) -> dict:
    if command == "budget":
        pack = result.pack
        payload = {
            "schema_version": 2,
            "command": command,
            "status": result.status,
            "reason_codes": list(result.reason_codes),
        }
        if pack is not None:
            payload["composition"] = pack.metrics.model_dump(mode="json")
        return payload
    payload = {
        "schema_version": 2,
        "command": command,
        **_composition_payload(result),
    }
    pack = result.pack
    if pack is None:
        return payload
    if command == "build":
        payload["model"] = pack.model
        payload["map_rows"] = [item.model_dump(mode="json") for item in pack.map_rows]
        payload["fragments"] = [
            item.model_dump(mode="json") for item in pack.fragments
        ]
        payload["included_fragment_ids"] = list(pack.included_fragment_ids)
        payload["prior_fragment_ids"] = list(pack.prior_fragment_ids)
        payload["source_revisions"] = [
            item.model_dump(mode="json") for item in pack.source_revisions
        ]
        return payload
    payload["model"] = pack.model
    payload["map_rows"] = [
        {
            "row_id": item.row_id,
            "path": item.path,
            "module": item.module,
            "digest": item.digest,
            "origin": item.origin,
        }
        for item in pack.map_rows
    ]
    payload["fragments"] = [
        item.model_dump(mode="json", exclude={"text"}) for item in pack.fragments
    ]
    payload["included_fragment_ids"] = list(pack.included_fragment_ids)
    payload["prior_fragment_ids"] = list(pack.prior_fragment_ids)
    payload["source_revisions"] = [
        item.model_dump(mode="json") for item in pack.source_revisions
    ]
    payload["candidate_refs"] = [
        item.model_dump(mode="json") for item in pack.candidate_refs
    ]
    payload["relation_evidence"] = [
        item.model_dump(mode="json") for item in pack.relation_evidence
    ]
    return payload


def _packed_human(
    result: PackResult, *, command: Literal["build", "inspect", "budget"]
) -> str:
    pack = result.pack
    if pack is None:
        return "\n".join(
            [
                f"status={result.status}",
                "reasons=" + ",".join(result.reason_codes),
            ]
        )
    metrics = pack.metrics
    header = (
        f"status={pack.status} level={pack.level} "
        f"input={metrics.input_tokens} output={metrics.output_tokens} "
        f"total={metrics.total_tokens}/{metrics.total_tokens + metrics.remaining_total} "
        f"remaining={metrics.remaining_total} fragments={len(pack.fragments)} "
        f"map={len(pack.map_rows)}"
    )
    if command == "budget":
        limits = " ".join(
            f"{name}={value}" for name, value in sorted(metrics.section_limits.items())
        )
        return "\n".join(
            [
                header,
                "sections="
                + " ".join(
                    f"{name}={value}"
                    for name, value in sorted(metrics.section_tokens.items())
                ),
                f"limits: {limits}",
                f"cumulative={metrics.cumulative_source_units} estimator={metrics.estimator}",
                "reasons=" + ",".join(pack.reason_codes),
            ]
        )
    lines = [header]
    if pack.omissions:
        lines.append(
            "omissions="
            + ",".join(item.reason for item in pack.omissions)
        )
    for row in pack.map_rows:
        if command == "build":
            lines.append(row.row)
        else:
            lines.append(f"map {row.row_id} {row.path} {row.digest or ''}".rstrip())
    for fragment in pack.fragments:
        if command == "build":
            lines.append(
                f"--- {fragment.path}:{fragment.start_line}-{fragment.end_line} "
                f"{fragment.digest} {fragment.kind} ---"
            )
            lines.append(fragment.text)
        else:
            reasons = ",".join(fragment.reasons)
            lines.append(
                f"{fragment.path}:{fragment.start_line}-{fragment.end_line} "
                f"{fragment.digest} {fragment.kind} id={fragment.fragment_id} "
                f"{fragment.symbol or ''} {reasons}".rstrip()
            )
    return "\n".join(lines)


def _emit_packed(
    result: PackResult,
    *,
    command: Literal["build", "inspect", "budget"],
    json_output: bool,
) -> None:
    _emit(
        _packed_payload(result, command=command),
        json_output=json_output,
        human=_packed_human(result, command=command),
    )
    log.info(
        "[CLI.context.%s] mode=packed status=%s fragments=%s input=%s",
        command,
        result.status,
        len(result.pack.fragments) if result.pack else 0,
        result.pack.metrics.input_tokens if result.pack else 0,
    )
    if result.status == "stale_reference":
        raise typer.Exit(1)
    if (
        result.status == "insufficient_context"
        and "source_unavailable" in result.reason_codes
    ):
        raise typer.Exit(1)


def _hit_payload(hit: SearchHit) -> dict:
    return {"path": hit.path, "line": hit.line, "column": hit.column, "text": hit.text}


def _retrieval_hit_payload(hit) -> dict:
    return {
        "kind": hit.kind,
        "path": hit.path,
        "symbol_id": hit.symbol_id,
        "qualified_name": hit.qualified_name,
        "definition_start_line": hit.definition_start_line,
        "start_line": hit.start_line,
        "end_line": hit.end_line,
        "source_digest": hit.source_digest,
        "source_kind": hit.source_kind,
        "relevance_score": hit.relevance_score,
        "ranking_reasons": [
            {
                "signal": item.signal,
                "value": item.value,
                "weight": item.weight,
                "weighted_points": item.weighted_points,
            }
            for item in hit.ranking_reasons
        ],
        "related_dependencies": [
            {
                "direction": item.direction,
                "related_path": item.related_path,
                "importer_path": item.importer_path,
                "start_line": item.start_line,
                "end_line": item.end_line,
                "source_digest": item.source_digest,
            }
            for item in hit.related_dependencies
        ],
        "estimated_token_cost": hit.estimated_token_cost,
    }


def _retrieval_payload(
    result: RetrievalResult,
    *,
    command: Literal["search", "rank", "explain"],
    include_mode: bool = False,
) -> dict:
    payload = {
        "command": command,
        "task_kind": result.task_kind,
        "kind": result.kind,
        "top_k": result.top_k,
        "degraded_reasons": list(result.degraded_reasons),
        "truncated": result.truncated,
        "hits": [_retrieval_hit_payload(hit) for hit in result.hits],
    }
    if include_mode:
        payload["mode"] = "task-aware"
    return payload


def _run_task_aware(
    ctx: typer.Context,
    *,
    command: Literal["search", "rank", "explain"],
    query: str,
    task_kind: str | None,
    kind: str | None,
    top_k: int | None,
    json_output: bool,
) -> None:
    try:
        request = RetrievalRequest(
            query=query,
            task_kind=task_kind or "feature",
            kind=kind or "all",
            top_k=top_k,
        )
    except (InvalidContextRequest, ValidationError, ForbiddenContext) as exc:
        raise typer.BadParameter("Invalid retrieval request") from exc

    context = command_context(ctx)
    provider = create_retrieval_provider(context)
    result = provider.retrieve(request)
    payload = _retrieval_payload(
        result, command=command, include_mode=(command == "search")
    )
    if command == "search":
        human = "\n".join(
            f"{hit.path}:{hit.start_line}-{hit.end_line} {hit.qualified_name or ''}".rstrip()
            for hit in result.hits
        )
    elif command == "rank":
        human = "\n".join(
            f"{hit.relevance_score:4d} {hit.estimated_token_cost:4d} "
            f"{hit.path}:{hit.start_line}-{hit.end_line} {hit.qualified_name or ''}".rstrip()
            for hit in result.hits
        )
    else:
        lines: list[str] = []
        if result.degraded_reasons:
            lines.append("diagnostics: " + ",".join(result.degraded_reasons))
        if result.truncated:
            lines.append("truncated: true")
        for hit in result.hits:
            lines.append(
                f"{hit.relevance_score:4d} {hit.estimated_token_cost:4d} "
                f"{hit.path}:{hit.start_line}-{hit.end_line} {hit.qualified_name or ''}".rstrip()
            )
            for reason in hit.ranking_reasons:
                lines.append(
                    f"  {reason.signal} value={reason.value} weight={reason.weight} "
                    f"points={reason.weighted_points}"
                )
            for dep in hit.related_dependencies:
                lines.append(
                    f"  {dep.direction} {dep.related_path} via {dep.importer_path}:"
                    f"{dep.start_line}-{dep.end_line}"
                )
        if not result.hits and not lines:
            lines.append("diagnostics: none")
        human = "\n".join(lines)
    _emit(payload, json_output=json_output, human=human)
    log.info(
        "[CLI.context.%s] hits=%s degraded=%s truncated=%s",
        command,
        len(result.hits),
        len(result.degraded_reasons),
        result.truncated,
    )


@context_app.command("search")
def search_command(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Literal single-line search text."),
    path: str | None = typer.Option(None, "--path", help="Restrict to one eligible path."),
    limit: int | None = typer.Option(None, "--limit", help="Maximum hits (tightens configured cap)."),
    symbol: str | None = typer.Option(None, "--symbol", help="Exact symbol name lookup."),
    task_aware: bool = typer.Option(False, "--task-aware", help="Opt into hybrid task retrieval."),
    task_kind: str | None = typer.Option(None, "--task-kind", help="Retrieval task profile."),
    kind: str | None = typer.Option(None, "--kind", help="Result kind: all|file|symbol."),
    top_k: int | None = typer.Option(None, "--top-k", help="Maximum ranked hits."),
    json_output: bool = typer.Option(False, "--json", help="Stable JSON output."),
) -> None:
    """Search canonical snapshots or resolve an exact symbol name."""

    if task_aware:
        if path is not None or symbol is not None or limit is not None:
            raise typer.BadParameter(
                "--task-aware cannot be combined with --path, --symbol, or --limit"
            )
        _run_task_aware(
            ctx,
            command="search",
            query=query,
            task_kind=task_kind,
            kind=kind,
            top_k=top_k,
            json_output=json_output,
        )
        return
    if task_kind is not None or kind is not None or top_k is not None:
        raise typer.BadParameter(
            "--task-kind/--kind/--top-k require --task-aware (or use rank/explain)"
        )

    context = command_context(ctx)
    repository = _repository(context)
    if limit is not None and (limit <= 0 or limit > context.config.context.search_hits_max):
        raise typer.BadParameter("--limit must be positive and no larger than context.search_hits_max")
    selected_path = _path(path) if path is not None else None
    if selected_path is not None and not repository.is_eligible(selected_path):
        raise ForbiddenContext("Requested path is not available for context retrieval")

    if symbol is not None:
        _guard(symbol, what="Symbol name")
        if not symbol.strip():
            raise typer.BadParameter("--symbol must be non-empty")
        index = build_python_index(
            repository,
            paths=[selected_path] if selected_path is not None else None,
        )
        matches = index.lookup(symbol, path=selected_path)
        if limit is not None:
            matches = matches[:limit]
        payload = {
            "command": "search",
            "mode": "symbol",
            "symbol": symbol,
            "hits": [
                {
                    "path": item.path,
                    "start_line": item.start_line,
                    "end_line": item.end_line,
                    "name": item.simple_name,
                    "qualified_name": item.qualified_name,
                    "kind": item.kind.value,
                    "signature": item.signature,
                }
                for item in matches
            ],
        }
        human = "\n".join(
            f"{item.path}:{item.start_line}-{item.end_line} {item.qualified_name} "
            f"{item.signature or ''}".rstrip()
            for item in matches
        )
        _emit(payload, json_output=json_output, human=human)
        log.info("[CLI.context.search] mode=symbol hits=%s", len(matches))
        return

    result = literal_search(
        repository,
        _guard(query, what="Search query"),
        limit=limit,
        paths=[selected_path] if selected_path is not None else None,
    )
    hits = result.hits
    payload = {
        "command": "search",
        "mode": "literal",
        "backend": result.backend,
        "degraded_reason": result.degraded_reason,
        "truncated": result.truncated,
        "hits": [_hit_payload(hit) for hit in hits],
    }
    human = "\n".join(f"{hit.path}:{hit.line}:{hit.column + 1}: {hit.text}" for hit in hits)
    _emit(payload, json_output=json_output, human=human)
    log.info("[CLI.context.search] mode=literal hits=%s backend=%s", len(hits), result.backend)


@context_app.command("rank")
def rank_command(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Task-aware retrieval query."),
    task_kind: str | None = typer.Option(None, "--task-kind", help="Retrieval task profile."),
    kind: str | None = typer.Option(None, "--kind", help="Result kind: all|file|symbol."),
    top_k: int | None = typer.Option(None, "--top-k", help="Maximum ranked hits."),
    json_output: bool = typer.Option(False, "--json", help="Stable JSON output."),
) -> None:
    """Rank task-relevant files and definitions (offline, no LLM)."""

    _run_task_aware(
        ctx,
        command="rank",
        query=query,
        task_kind=task_kind,
        kind=kind,
        top_k=top_k,
        json_output=json_output,
    )


@context_app.command("explain")
def explain_command(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Task-aware retrieval query."),
    task_kind: str | None = typer.Option(None, "--task-kind", help="Retrieval task profile."),
    kind: str | None = typer.Option(None, "--kind", help="Result kind: all|file|symbol."),
    top_k: int | None = typer.Option(None, "--top-k", help="Maximum ranked hits."),
    json_output: bool = typer.Option(False, "--json", help="Stable JSON output."),
) -> None:
    """Explain ranked retrieval hits with reasons and diagnostics."""

    _run_task_aware(
        ctx,
        command="explain",
        query=query,
        task_kind=task_kind,
        kind=kind,
        top_k=top_k,
        json_output=json_output,
    )


def _parse_lines(value: str) -> tuple[int, int]:
    if ":" not in value:
        raise typer.BadParameter("--lines must be START:END")
    left, right = value.split(":", 1)
    try:
        start = int(left)
        end = int(right)
    except ValueError:
        raise typer.BadParameter("--lines must contain integers") from None
    if start < 1 or end < start:
        raise typer.BadParameter("--lines bounds must satisfy 1 <= START <= END")
    return start, end


@context_app.command("build")
def build_command(
    ctx: typer.Context,
    task: str = typer.Argument(..., help="Task description or packed retrieval query."),
    path: list[str] = typer.Option([], "--path", help="Explicit eligible path (repeatable)."),
    level: ExpansionLevel = typer.Option(ExpansionLevel.SYMBOLS, "--level"),
    reason: str | None = typer.Option(None, "--reason", help="Justification for full files."),
    indexed_map: bool = typer.Option(
        False, "--indexed-map", help="Opt-in compact map from the persistent code index."
    ),
    packed: bool = typer.Option(False, "--packed", help="Use V2 token-aware packing."),
    legacy: bool = typer.Option(False, "--legacy", help="Use the V1 builder explicitly."),
    pack_level: str = typer.Option("l1", "--pack-level", help="Packed level: l0 or l1."),
    json_output: bool = typer.Option(False, "--json", help="Stable JSON output."),
) -> None:
    """Build a deterministic context pack under configured budgets."""

    packed_mode = _resolve_packed_mode(
        ctx,
        packed=packed,
        legacy=legacy,
        pack_level=pack_level,
        legacy_options=("level", "path", "reason", "indexed_map"),
    )
    if packed_mode:
        result = _run_packed(ctx, query=task, level=pack_level)
        _emit_packed(result, command="build", json_output=json_output)
        return

    context = command_context(ctx)
    paths = [_path(item) for item in path]
    _guard(task, what="Task text")
    if indexed_map and level != ExpansionLevel.MAP:
        raise typer.BadParameter("--indexed-map requires --level map")
    if level == ExpansionLevel.FULL and (len(paths) != 1 or not reason or not reason.strip()):
        raise typer.BadParameter("--level full requires exactly one --path and a non-empty --reason")
    try:
        query = ContextQuery(
            task=task,
            level=level,
            paths=paths,
            reason=reason,
            allow_git=not indexed_map,
        )
    except ValidationError:
        raise typer.BadParameter("Invalid context selectors") from None

    if indexed_map:
        repository, repository_map = create_indexed_map_preview_pair(context)
        result = ContextBuilder(repository).build(query, indexed_map=repository_map)
    else:
        repository = _repository(context)
        for item in query.paths:
            if not repository.is_eligible(item):
                raise ForbiddenContext("Requested path is not available for context retrieval")
        result = ContextBuilder(repository).build(query)
    pack = result.pack
    payload = {
        "command": "build",
        "level": pack.level.value,
        "estimated_tokens": pack.estimated_tokens,
        "required_tokens": pack.required_tokens,
        "relevant_paths": pack.relevant_paths,
        "symbols": [item.model_dump(mode="json") for item in pack.symbols],
        "excerpts": [item.model_dump(mode="json") for item in pack.excerpts],
        "repository_map": pack.repository_map,
        "digests": [item.model_dump(mode="json") for item in pack.digests],
        "omissions": [item.model_dump(mode="json") for item in pack.omissions],
    }
    header = (
        f"level={pack.level.value} tokens={pack.estimated_tokens}/{context.config.context.stage_prompt_tokens_max} "
        f"files={len(pack.relevant_paths)} excerpts={len(pack.excerpts)} omissions={len(pack.omissions)}"
    )
    body = [header]
    if indexed_map and pack.repository_map:
        body.extend(pack.repository_map)
    for excerpt in pack.excerpts:
        body.append(
            f"--- {excerpt.path}:{excerpt.start_line}-{excerpt.end_line} "
            f"{excerpt.digest} {excerpt.kind} ---"
        )
        body.append(excerpt.text)
    _emit(payload, json_output=json_output, human="\n".join(body))
    log.info(
        "[CLI.context.build] level=%s excerpts=%s tokens=%s",
        pack.level.value,
        len(pack.excerpts),
        pack.estimated_tokens,
    )


@context_app.command("inspect")
def inspect_command(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Eligible path, or packed retrieval query."),
    symbol: str | None = typer.Option(None, "--symbol", help="Exact symbol name."),
    lines: str | None = typer.Option(None, "--lines", help="START:END line interval."),
    packed: bool = typer.Option(False, "--packed", help="Use V2 token-aware packing."),
    legacy: bool = typer.Option(False, "--legacy", help="Use V1 path inspection explicitly."),
    pack_level: str = typer.Option("l1", "--pack-level", help="Packed level: l0 or l1."),
    json_output: bool = typer.Option(False, "--json", help="Stable JSON output."),
) -> None:
    """Inspect safe metadata or one provenance-bearing selected range."""

    packed_mode = _resolve_packed_mode(
        ctx,
        packed=packed,
        legacy=legacy,
        pack_level=pack_level,
        legacy_options=("symbol", "lines"),
    )
    if packed_mode:
        result = _run_packed(ctx, query=path, level=pack_level)
        _emit_packed(result, command="inspect", json_output=json_output)
        return

    context = command_context(ctx)
    repository = _repository(context)
    selected_path = _path(path)
    if symbol is not None and lines is not None:
        raise typer.BadParameter("--symbol and --lines are mutually exclusive")
    if not repository.is_eligible(selected_path):
        raise ForbiddenContext("Requested path is not available for context retrieval")
    snapshot = repository.read(selected_path)
    index = build_python_index(
        repository, paths=[selected_path] if is_python(selected_path) else []
    )
    symbols = index.symbols_for(selected_path)
    if symbol is not None:
        _guard(symbol, what="Symbol name")
        matches = index.lookup(symbol, path=selected_path)
        if not matches:
            raise InvalidContextRequest("Requested symbol was not found in the selected file")
        if len(matches) > 1:
            # Preserve deterministic first match by coordinates.
            matches = [matches[0]]
        query = ContextQuery(
            task=f"inspect {symbol}",
            level=ExpansionLevel.RANGES,
            paths=[selected_path],
            symbols=[symbol],
            scope_paths=[selected_path],
            exact_symbols_only=True,
            stage=None,
        )
        result = ContextBuilder(repository, index=index).build(query)
        payload = {
            "command": "inspect",
            "mode": "symbol",
            "path": selected_path,
            "symbols": [item.model_dump(mode="json") for item in matches],
            "pack": result.pack.model_dump(mode="json"),
        }
        human = "\n".join(
            f"{item.path}:{item.start_line}-{item.end_line} {item.symbol or ''} {item.text}"
            for item in result.pack.excerpts
        )
        _emit(payload, json_output=json_output, human=human or "no source")
        log.info("[CLI.context.inspect] mode=symbol excerpts=%s", len(result.pack.excerpts))
        return
    if lines is not None:
        start, end = _parse_lines(lines)
        if end > snapshot.lines:
            raise typer.BadParameter("--lines bounds are outside the selected file")
        query = ContextQuery(
            task=f"inspect {selected_path}:{start}-{end}",
            level=ExpansionLevel.RANGES,
            paths=[selected_path],
            scope_paths=[selected_path],
            line_range=SourceRange(path=selected_path, start_line=start, end_line=end),
        )
        result = ContextBuilder(repository, index=index).build(query)
        payload = {
            "command": "inspect",
            "mode": "lines",
            "path": selected_path,
            "line_range": {"start": start, "end": end},
            "pack": result.pack.model_dump(mode="json"),
        }
        excerpt = result.pack.excerpts[0].text if result.pack.excerpts else ""
        _emit(payload, json_output=json_output, human=excerpt)
        log.info("[CLI.context.inspect] mode=lines excerpts=%s", len(result.pack.excerpts))
        return

    payload = {
        "command": "inspect",
        "mode": "metadata",
        "path": selected_path,
        "digest": snapshot.digest,
        "chars": len(snapshot.text),
        "lines": snapshot.lines,
        "symbols": [item.model_dump(mode="json") for item in symbols],
    }
    human = "\n".join(
        [
            f"{selected_path} {snapshot.digest} chars={len(snapshot.text)} lines={snapshot.lines}",
            *[
                f"{item.start_line}-{item.end_line} {item.qualified_name} {item.signature or ''}".rstrip()
                for item in symbols
            ],
        ]
    )
    _emit(payload, json_output=json_output, human=human)
    log.info("[CLI.context.inspect] mode=metadata symbols=%s", len(symbols))


@context_app.command("budget")
def budget_command(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Packed retrieval query."),
    pack_level: str = typer.Option("l1", "--pack-level", help="Packed level: l0 or l1."),
    json_output: bool = typer.Option(False, "--json", help="Stable JSON output."),
) -> None:
    """Show numeric packed-context limits and composition only."""

    result = _run_packed(ctx, query=query, level=pack_level)
    _emit_packed(result, command="budget", json_output=json_output)
