import json
import os

import typer
import yaml

from lean_weave.logging_setup import (
    SecretRedactionFilter,
    get_logger,
    redact_console_message,
)
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
        rendered = json.dumps(data)
    else:
        rendered = yaml.safe_dump(data, sort_keys=False)
    # Configuration strings are operator input; redact known secret values even
    # when they were embedded in a URL or another field.
    configured = SecretRedactionFilter(
        ("DEEPSEEK_API_KEY", context.config.llm.api_key_env)
    ).redact(redact_console_message(rendered))
    typer.echo(configured, nl=format != "yaml")
    get_logger("cli").info("[CLI.config.show] exit_code=0")
