import json
import os

import typer
import yaml

from lean_weave.logging_setup import get_logger
from lean_weave.cli.runtime import command_context
from lean_weave.cli.options import OutputFormat

config_app = typer.Typer(help="Inspect product configuration.", no_args_is_help=True)


@config_app.command("show")
def show(
    ctx: typer.Context,
    format: OutputFormat = typer.Option(OutputFormat.YAML, "--format"),
) -> None:
    """Print file/default settings, never secret values."""
    context = command_context(ctx)
    data = context.config.model_dump(mode="json")
    data["api_key_present"] = bool(os.getenv(context.config.llm.api_key_env))
    if format == "json":
        typer.echo(json.dumps(data))
    else:
        typer.echo(yaml.safe_dump(data, sort_keys=False), nl=False)
    get_logger("cli").info("[CLI.config.show] exit_code=0")
