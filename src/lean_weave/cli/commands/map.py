"""Offline repository-map queries over the checked persistent index."""

from __future__ import annotations

from pathlib import Path

import typer

from lean_weave.app import create_repository_map
from lean_weave.cli.runtime import command_context
from lean_weave.errors import CodeIndexError, RepositoryMapError
from lean_weave.logging_setup import get_logger

log = get_logger("cli.map")

map_app = typer.Typer(
    help="Query compact repository maps from the offline source index (no LLM calls).",
    no_args_is_help=True,
)

_INDEX_HINTS = {
    "missing": "run 'wl index build'",
    "stale": "run 'wl index update'",
    "busy": "retry after the other index operation completes",
    "corrupt": "run 'wl index clear --yes' then 'wl index build'",
    "incompatible": "run 'wl index clear --yes' then 'wl index build'",
    "recovery_required": "run 'wl index clear --yes' then 'wl index build'",
    "incomplete": "retry after the working tree stops changing",
    "storage_unavailable": "retry when the index is stable",
    "unsafe_cache": "choose a private cache location",
    "root_unavailable": "run from an authorized checkout",
}


def _fail_index(exc: CodeIndexError) -> None:
    typer.echo(f"Error: {exc.code}", err=True)
    hint = _INDEX_HINTS.get(exc.code)
    if hint:
        typer.echo(f"hint: {hint}", err=True)
    raise typer.Exit(1)


def _fail_map(exc: RepositoryMapError) -> None:
    typer.echo(f"Error: {exc.code}", err=True)
    if exc.code == "invalid_selector":
        raise typer.Exit(2)
    raise typer.Exit(1)


def _service(context):
    return create_repository_map(context, workspace_root=Path.cwd())


def _emit(result) -> None:
    # Exact rendered text only — no extra banner or unbudgeted newline.
    typer.echo(result.text, nl=False)
    log.debug(
        "[CLI.map] rows=%s tokens=%s truncated=%s",
        len(result.rows),
        result.estimated_tokens,
        result.truncated,
    )


@map_app.command("show")
def show_command(ctx: typer.Context) -> None:
    """Print a globally ranked compact repository map."""

    context = command_context(ctx)
    try:
        result = _service(context).show()
    except RepositoryMapError as exc:
        _fail_map(exc)
    except CodeIndexError as exc:
        _fail_index(exc)
    _emit(result)


@map_app.command("module")
def module_command(
    ctx: typer.Context,
    module: str = typer.Argument(..., help="Exact indexed module name."),
) -> None:
    """Print a one-hop map centered on one unique module."""

    context = command_context(ctx)
    try:
        result = _service(context).module(module)
    except RepositoryMapError as exc:
        _fail_map(exc)
    except CodeIndexError as exc:
        _fail_index(exc)
    _emit(result)


@map_app.command("dependencies")
def dependencies_command(
    ctx: typer.Context,
    module: str = typer.Argument(..., help="Exact indexed module name."),
) -> None:
    """Print direct incoming and outgoing import evidence for one module."""

    context = command_context(ctx)
    try:
        result = _service(context).dependencies(module)
    except RepositoryMapError as exc:
        _fail_map(exc)
    except CodeIndexError as exc:
        _fail_index(exc)
    _emit(result)


@map_app.command("references")
def references_command(
    ctx: typer.Context,
    symbol: str = typer.Argument(..., help="Simple or fully qualified symbol name."),
) -> None:
    """Print definition and static import evidence for one symbol group."""

    context = command_context(ctx)
    try:
        result = _service(context).references(symbol)
    except RepositoryMapError as exc:
        _fail_map(exc)
    except CodeIndexError as exc:
        _fail_index(exc)
    _emit(result)
