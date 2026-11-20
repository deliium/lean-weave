"""Public feature workflow commands."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from lean_weave.errors import ConfigError, LeanWeaveError, WorktreeError
from lean_weave.logging_setup import get_logger
from lean_weave.workflow.session import FeatureSession, FeatureView

log = get_logger("cli.feature")


def _options(ctx: typer.Context) -> dict:
    root = ctx.find_root()
    return root.obj if isinstance(root.obj, dict) else {}


def _print_view(view: FeatureView) -> None:
    typer.echo(f"feature_id={view.feature_id}")
    typer.echo(f"status={view.status}")
    typer.echo(f"stage={view.stage}")
    if view.reason:
        typer.echo(f"reason={view.reason}")
    if view.worktree_path:
        typer.echo(f"worktree={view.worktree_path}")
    if view.branch:
        typer.echo(f"branch={view.branch}")
    if view.archived:
        typer.echo("archived=true")
        if view.archive_path:
            typer.echo(f"archive={view.archive_path}")
    typer.echo(f"next_action={view.next_action}")
    typer.echo(f"changes={view.change_summary or view.change_state}")
    if view.cancel_requested:
        typer.echo("cancel_requested=true")
    if view.plan_goal:
        typer.echo(f"plan_goal={view.plan_goal}")
    for task_id, title, files in view.plan_tasks[:32]:
        typer.echo(f"plan_task={task_id}: {title} [{', '.join(files)}]")
    if view.plan_digest:
        typer.echo(f"plan_digest={view.plan_digest}")
    if view.approval_command:
        typer.echo(f"approve={view.approval_command}")
    elif view.approval_token:
        typer.echo(f"approval_token={view.approval_token}")
    if view.modified_paths:
        typer.echo(f"modified={len(view.modified_paths)}")
        for path in view.modified_paths[:32]:
            typer.echo(f"  {path}")
    if view.workflow is not None:
        verification = view.workflow.verification
        if verification is not None:
            typer.echo(
                f"verification={verification.status.value} blockers={len(verification.blockers)}"
            )
        counters = view.workflow.counters
        total = counters.prompt_tokens + counters.completion_tokens
        typer.echo(f"llm_calls={counters.llm_calls}")
        typer.echo(
            "usage="
            f"prompt:{counters.prompt_tokens},"
            f"completion:{counters.completion_tokens},"
            f"reasoning:{counters.reasoning_tokens},"
            f"total:{total}"
        )


def feature_command(
    ctx: typer.Context,
    description: Optional[str] = typer.Argument(None),
    auto: bool = typer.Option(False, "--auto"),
    review: bool = typer.Option(False, "--review"),
    worktree: Optional[Path] = typer.Option(None, "--worktree"),
) -> None:
    """Start a feature inside an isolated Git worktree."""

    if description is None:
        typer.echo(ctx.get_help())
        return
    if auto == review:
        raise ConfigError("exactly_one_of_auto_or_review")
    config_path = _options(ctx).get("config_path")
    if config_path is None:
        raise ConfigError("operator_config_required")
    mode = "auto" if auto else "review"
    log.debug("[CLI.feature] command=start mode=%s", mode)
    view = FeatureSession().start(
        description,
        mode=mode,
        config_path=Path(config_path),
        adopt_worktree=worktree,
    )
    _print_view(view)


def status_command(feature_id: str) -> None:
    view = FeatureSession().status(feature_id)
    log.debug("[CLI.feature] command=status category=%s", view.status)
    _print_view(view)


def result_command(feature_id: str) -> None:
    view = FeatureSession().result(feature_id)
    log.debug("[CLI.feature] command=result category=%s", view.status)
    _print_view(view)


def cancel_command(feature_id: str) -> None:
    view = FeatureSession().cancel(feature_id)
    log.debug("[CLI.feature] command=cancel category=%s", view.status)
    _print_view(view)


def resume_command(
    ctx: typer.Context,
    feature_id: str,
    approve: Optional[str] = typer.Option(None, "--approve"),
) -> None:
    config_path = _options(ctx).get("config_path")
    session = FeatureSession()
    peek = None
    try:
        peek = session.status(feature_id)
    except LeanWeaveError:
        pass
    terminal = peek is not None and (
        peek.status
        in {
            "success",
            "failed",
            "cancelled",
            "budget_exhausted",
        }
        or peek.cancel_requested
    )
    if terminal:
        view = session.resume(feature_id, config_path=None, approve_token=approve)
    else:
        if config_path is None:
            raise ConfigError("operator_config_required")
        view = session.resume(
            feature_id,
            config_path=Path(config_path),
            approve_token=approve,
        )
    log.debug("[CLI.feature] command=resume category=%s", view.status)
    _print_view(view)


worktree_app = typer.Typer(help="Inspect or clean up managed feature worktrees.")


@worktree_app.command("inspect")
def worktree_inspect(feature_id: str) -> None:
    info = FeatureSession().inspect_worktree(feature_id)
    for key, value in info.items():
        typer.echo(f"{key}={value}")


@worktree_app.command("cleanup")
def worktree_cleanup(
    feature_id: str,
    confirm: bool = typer.Option(False, "--confirm"),
) -> None:
    if not confirm:
        raise WorktreeError("cleanup_unconfirmed")
    info = FeatureSession().cleanup_worktree(feature_id, confirm=True)
    for key, value in info.items():
        typer.echo(f"{key}={value}")
