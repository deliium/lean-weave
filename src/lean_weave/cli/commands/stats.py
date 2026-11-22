"""Read-only token and context statistics commands."""

from __future__ import annotations

import json

import typer

from lean_weave.logging_setup import get_logger
from lean_weave.workflow.session import FeatureSession
from lean_weave.workflow.stats import FeatureStats

log = get_logger("cli.stats")

stats_app = typer.Typer(
    help="Read-only workflow usage and context statistics.",
    invoke_without_command=True,
    no_args_is_help=False,
)


def _fmt(value: int | None) -> str:
    return "unavailable" if value is None else str(value)


def _evidence_label(stats: FeatureStats) -> str:
    usage = stats.usage or {}
    if not stats.available:
        return f"unavailable ({usage.get('limit_reason') or 'journal_unavailable'})"
    if stats.partial_reason:
        return f"partial ({stats.partial_reason})"
    return "complete"


def _print_feature(stats: FeatureStats) -> None:
    totals = (stats.usage or {}).get("totals") or {}
    typer.echo(f"feature_id={stats.feature_id}")
    typer.echo(f"status={stats.status}")
    typer.echo(f"stage={stats.stage}")
    if stats.reason:
        typer.echo(f"reason={stats.reason}")
    typer.echo(f"archived={'true' if stats.archived else 'false'}")
    typer.echo(f"evidence={_evidence_label(stats)}")
    typer.echo(f"workflow_retries={_fmt(stats.workflow_retry_count)}")
    typer.echo(f"verification={stats.verification_status or 'unavailable'}")
    typer.echo(f"active_ms={_fmt(stats.active_ms_total)}")
    typer.echo(
        "tokens="
        f"input:{_fmt(totals.get('input_tokens'))},"
        f"output:{_fmt(totals.get('output_tokens'))},"
        f"total:{_fmt(totals.get('total_tokens'))},"
        f"cache:{_fmt(totals.get('cache_hit_tokens'))},"
        f"reasoning:{_fmt(totals.get('reasoning_tokens'))}"
    )
    typer.echo(
        f"attempts={totals.get('attempts', 0)} "
        f"logical_calls={totals.get('logical_calls', 0)} "
        f"retry_attempts={totals.get('retry_attempts', 0)} "
        f"failed={totals.get('failed_attempts', 0)} "
        f"unresolved={totals.get('unresolved_attempts', 0)}"
    )
    typer.echo(
        f"budget_charge={_fmt(totals.get('budget_charge'))} "
        f"measured={_fmt(stats.usage.get('measured_charge'))} "
        f"estimated={_fmt(stats.usage.get('estimated_charge'))} "
        f"unresolved={_fmt(stats.usage.get('unresolved_charge'))}"
    )
    for stage in stats.stages:
        typer.echo(
            f"stage={stage['name']} attempts={stage['attempts']} "
            f"tokens={_fmt(stage['total_tokens'])}"
        )
    for task in stats.tasks:
        typer.echo(
            f"task={task['id']} status={task['status']} "
            f"tokens={task['tokens']} calls={task['calls']}"
        )
    context = stats.context or {}
    if context.get("available"):
        typer.echo(
            "context="
            f"files:{_fmt(context.get('selected_files_max'))},"
            f"symbols:{_fmt(context.get('selected_symbols_max'))},"
            f"pack_tokens:{_fmt(context.get('pack_input_tokens_max'))},"
            f"retrieval_ms:{_fmt(context.get('retrieval_ms_total'))},"
            f"cache:{context.get('cache_hits', 0)}/{context.get('cache_misses', 0)}"
        )
    else:
        typer.echo("context=unavailable")


def _inventory_payload(
    features: list[FeatureStats], failures: list[dict[str, str]]
) -> dict:
    return {
        "schema_version": 1,
        "features": [item.to_dict() for item in features],
        "failures": failures,
        "partial": bool(failures),
    }


@stats_app.callback()
def stats_default(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show checkout-wide feature statistics."""

    if ctx.invoked_subcommand is not None:
        return
    features, failures = FeatureSession().stats_all()
    log.debug("[CLI.stats] scope=all features=%s failures=%s", len(features), len(failures))
    if json_output:
        typer.echo(
            json.dumps(
                _inventory_payload(features, failures),
                sort_keys=True,
                ensure_ascii=False,
            )
        )
    else:
        typer.echo("feature_id|status|evidence|total_tokens|attempts|retries")
        for item in features:
            totals = (item.usage or {}).get("totals") or {}
            typer.echo(
                f"{item.feature_id}|{item.status}|{_evidence_label(item)}|"
                f"{_fmt(totals.get('total_tokens'))}|{totals.get('attempts', 0)}|"
                f"{_fmt(item.workflow_retry_count)}"
            )
        for failure in failures:
            typer.echo(
                f"{failure['feature_id']}|unavailable|{failure['reason']}|||",
                err=True,
            )
    if failures:
        raise typer.Exit(1)


@stats_app.command("feature")
def stats_feature(
    feature_id: str,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show one feature's token, stage, task and context statistics."""

    stats = FeatureSession().stats(feature_id)
    log.debug(
        "[CLI.stats] scope=feature feature=%s available=%s partial=%s",
        stats.feature_id,
        stats.available,
        stats.partial_reason,
    )
    if json_output:
        typer.echo(json.dumps(stats.to_dict(), sort_keys=True, ensure_ascii=False))
    else:
        _print_feature(stats)
