"""Explicit offline maintenance for the disposable persistent source index."""

from __future__ import annotations

from pathlib import Path

import typer

from lean_weave.app import create_code_index
from lean_weave.cli.runtime import command_context
from lean_weave.code_index import StoreHealth
from lean_weave.errors import CodeIndexError
from lean_weave.logging_setup import get_logger

log = get_logger("cli.index")

index_app = typer.Typer(
    help="Build, update, inspect, or clear the offline source index (no LLM calls).",
    no_args_is_help=True,
)

_HINTS = {
    "missing": "run 'wl index build'",
    "busy": "retry after the other index operation completes",
    "corrupt": "run 'wl index clear --yes' then 'wl index build'",
    "incompatible": "run 'wl index clear --yes' then 'wl index build'",
    "recovery_required": "run 'wl index clear --yes' then 'wl index build'",
    "publication_uncertain": "recheck with 'wl index status'",
    "clear_incomplete": "retry 'wl index clear --yes'",
    "incomplete": "retry after the working tree stops changing",
    "unsafe_facts": "remove sensitive values or exclude the source",
    "unsafe_cache": "choose a private cache location",
    "storage_unavailable": "retry the operation",
    "capacity_exceeded": "reduce the indexed source set",
    "root_unavailable": "run from an authorized checkout",
}


def _fail(exc: CodeIndexError) -> None:
    typer.echo(f"Error: {exc.code}", err=True)
    hint = _HINTS.get(exc.code)
    if hint:
        typer.echo(f"hint: {hint}", err=True)
    raise typer.Exit(1)


def _index(context, *, cache_base: Path | None = None):
    return create_code_index(context, workspace_root=Path.cwd(), cache_base=cache_base)


def _summary(action: str, update) -> str:
    counts = update.counts
    ordered = (
        "added",
        "modified",
        "moved",
        "removed",
        "unchanged",
        "invalidated",
        "parsed",
        "syntax_failed",
    )
    values = " ".join(f"{key}={int(counts.get(key, 0))}" for key in ordered)
    return f"index {action} revision={update.revision} schema={update.schema_version} {values}"


@index_app.command("build")
def build_command(ctx: typer.Context) -> None:
    """Create or refresh the current generation (parses changed files only)."""

    context = command_context(ctx)
    try:
        update = _index(context).build()
    except CodeIndexError as exc:
        _fail(exc)
    typer.echo(_summary("built", update))
    log.info("[CLI.index.build] revision=%s files=%s", update.revision, update.count("unchanged"))


@index_app.command("update")
def update_command(ctx: typer.Context) -> None:
    """Refresh an existing generation; refuse when no generation exists."""

    context = command_context(ctx)
    try:
        update = _index(context).update()
    except CodeIndexError as exc:
        _fail(exc)
    typer.echo(_summary("updated", update))
    log.info("[CLI.index.update] revision=%s", update.revision)


@index_app.command("status")
def status_command(ctx: typer.Context) -> None:
    """Report storage health and source freshness without parsing or writing."""

    context = command_context(ctx)
    try:
        status = _index(context).status()
    except CodeIndexError as exc:
        _fail(exc)
    revision = status.revision or "-"
    schema = "-" if status.schema_version is None else str(status.schema_version)
    files = "-" if status.files is None else str(status.files)
    symbols = "-" if status.symbols is None else str(status.symbols)
    imports = "-" if status.imports is None else str(status.imports)
    typer.echo(
        f"index health={status.health.value} freshness={status.freshness.value} "
        f"revision={revision} schema={schema} files={files} symbols={symbols} imports={imports}"
    )
    log.info(
        "[CLI.index.status] health=%s freshness=%s",
        status.health.value,
        status.freshness.value,
    )
    if status.health in {StoreHealth.INCOMPATIBLE, StoreHealth.CORRUPT, StoreHealth.UNAVAILABLE}:
        hint = _HINTS.get(status.health.value)
        if hint:
            typer.echo(f"hint: {hint}", err=True)
        raise typer.Exit(1)


@index_app.command("clear")
def clear_command(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", help="Confirm removal of this checkout's cache."),
) -> None:
    """Remove this checkout's disposable cache after explicit confirmation."""

    if not yes:
        typer.echo("Error: 'index clear' requires --yes", err=True)
        raise typer.Exit(2)
    context = command_context(ctx)
    try:
        removed = _index(context).clear()
    except CodeIndexError as exc:
        _fail(exc)
    typer.echo("cleared" if removed else "missing")
    log.info("[CLI.index.clear] outcome=%s", "cleared" if removed else "missing")
