"""Explicit offline maintenance for the disposable context cache."""

from __future__ import annotations

from pathlib import Path

import typer

from lean_weave.app import create_context_cache
from lean_weave.cli.runtime import command_context
from lean_weave.context_cache.maintenance import rebuild_context_cache
from lean_weave.context_cache.models import CacheHealth
from lean_weave.errors import CodeIndexError
from lean_weave.logging_setup import get_logger

log = get_logger("cli.cache")

cache_app = typer.Typer(
    help="Inspect, clear, or rebuild the offline context cache (no LLM calls).",
    no_args_is_help=True,
)

_HINTS = {
    "busy": "retry after the other cache operation completes",
    "corrupt": "run 'wl cache clear --yes' then 'wl cache rebuild'",
    "incompatible": "run 'wl cache clear --yes' then 'wl cache rebuild'",
    "unsafe_cache": "choose a private cache location",
    "capacity_exceeded": "run 'wl cache rebuild' under the current limits",
    "publication_uncertain": "recheck with 'wl cache status'",
    "clear_incomplete": "retry 'wl cache clear --yes'",
    "incomplete": "retry after the working tree stops changing",
}

_FAILED = {
    CacheHealth.UNSAFE,
    CacheHealth.CORRUPT,
    CacheHealth.INCOMPATIBLE,
    CacheHealth.BUSY,
    CacheHealth.CAPACITY,
    CacheHealth.UNAVAILABLE,
}


def _yaml(value: str | None) -> str:
    return value if value else "-"


@cache_app.command("status")
def status_command(ctx: typer.Context) -> None:
    """Report health and bounded storage information without parsing source."""

    context = command_context(ctx)
    cache = create_context_cache(context, workspace_root=Path.cwd())
    status = cache.status()
    typer.echo(
        f"cache enabled={str(status.enabled).lower()} health={status.health.value} "
        f"schema={_yaml(None if status.schema_version is None else str(status.schema_version))} "
        f"entries={_yaml(None if status.entries is None else str(status.entries))} "
        f"db_bytes={_yaml(None if status.db_bytes is None else str(status.db_bytes))} "
        f"max_entries={status.max_entries} max_bytes={status.max_bytes} "
        f"migration={'pending' if status.migration_pending else 'none'}"
    )
    log.info("[CLI.cache.status] health=%s", status.health.value)
    if status.health in _FAILED:
        _exit(path=f"cache_{status.health.value}")


def _exit(*, path: str) -> None:
    code, _, reason = path.partition("_")
    hint = _HINTS.get(reason)
    typer.echo(f"Error: {reason or 'unavailable'}", err=True)
    if hint:
        typer.echo(f"hint: {hint}", err=True)
    raise typer.Exit(1)


@cache_app.command("stats")
def stats_command(ctx: typer.Context) -> None:
    """Show fixed ordered cache counters without reading source."""

    context = command_context(ctx)
    cache = create_context_cache(context, workspace_root=Path.cwd())
    stats = cache.stats()
    ordered = (
        "hit",
        "miss",
        "invalidation",
        "rebuild",
        "corrupt_entry",
        "eviction",
        "bypass",
    )
    totals = " ".join(f"{event}={stats.count(event)}" for event in ordered)
    typer.echo(f"cache stats {totals}")
    for kind, events in stats.kind_events:
        values = " ".join(f"{event}={count}" for event, count in events)
        typer.echo(f"kind {kind} {values}")
    log.info("[CLI.cache.stats] health=%s", stats.health.value)
    if stats.health in _FAILED:
        _exit(path=f"cache_{stats.health.value}")


@cache_app.command("clear")
def clear_command(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", help="Confirm removal of this checkout's cache."),
) -> None:
    """Remove this checkout's context-cache data after explicit confirmation."""

    if not yes:
        typer.echo("Error: 'cache clear' requires --yes", err=True)
        raise typer.Exit(2)
    context = command_context(ctx)
    cache = create_context_cache(context, workspace_root=Path.cwd(), force=True)
    try:
        removed = cache.clear()
    except CodeIndexError as exc:
        typer.echo(f"Error: {exc.code}", err=True)
        hint = _HINTS.get(exc.code)
        if hint:
            typer.echo(f"hint: {hint}", err=True)
        raise typer.Exit(1) from None
    typer.echo("cleared" if removed else "missing")
    log.info("[CLI.cache.clear] outcome=%s", "cleared" if removed else "missing")


@cache_app.command("rebuild")
def rebuild_command(ctx: typer.Context) -> None:
    """Seed a fresh cache from one strict scan; never edits the source index."""

    context = command_context(ctx)
    if not context.config.context.cache.enabled:
        typer.echo("Error: context cache is disabled", err=True)
        typer.echo("hint: set context.cache.enabled: true in .lean-weave/config.yaml", err=True)
        raise typer.Exit(2)
    try:
        status, report = rebuild_context_cache(
            context, workspace_root=Path.cwd()
        )
    except CodeIndexError as exc:
        typer.echo(f"Error: {exc.code}", err=True)
        hint = _HINTS.get(exc.code)
        if hint:
            typer.echo(f"hint: {hint}", err=True)
        raise typer.Exit(1) from None
    typer.echo(
        f"cache rebuilt health={status.health.value} entries={status.entries} "
        f"facts={report.facts} spans={report.spans} imports={report.imports} "
        f"map_seed={report.map_seed} capacity_skips={report.capacity_skips}"
    )
    log.info("[CLI.cache.rebuild] health=%s", status.health.value)
