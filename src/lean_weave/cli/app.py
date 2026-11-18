"""Thin command routing; help and initialization do not load configuration."""

from pathlib import Path

import typer
from typer.core import TyperGroup

from lean_weave.errors import LeanWeaveError
from lean_weave.logging_setup import get_logger, redact_console_message
from lean_weave.cli.commands.config import config_app
from lean_weave.cli.commands.doctor import doctor
from lean_weave.cli.commands.feature import feature_app
from lean_weave.cli.commands.init import initialize
from lean_weave.cli.options import LogFormat

log = get_logger("cli")


class SafeGroup(TyperGroup):
    def invoke(self, ctx):
        try:
            result = super().invoke(ctx)
        except LeanWeaveError as exc:
            # Only our safe error messages, never raw parser/validation exceptions.
            log.error("[CLI.run] %s exit_code=%s", exc, exc.exit_code)
            log.debug("[FIX] CLI error output redaction applied")
            typer.echo(f"Error: {redact_console_message(str(exc))}", err=True)
            raise typer.Exit(exc.exit_code) from None
        return result


app = typer.Typer(name="wl", cls=SafeGroup, no_args_is_help=True, pretty_exceptions_enable=False)
app.command("init")(initialize)
app.command("doctor")(doctor)
app.add_typer(config_app, name="config")
app.add_typer(feature_app, name="feature")


@app.callback()
def startup(
    ctx: typer.Context,
    config: Path | None = typer.Option(None, "--config"),
    log_level: str | None = typer.Option(None, "--log-level"),
    log_format: LogFormat | None = typer.Option(None, "--log-format"),
) -> None:
    # Context is built lazily by commands, after Click processes nested help.
    ctx.obj = {
        "config_path": config, "log_level": log_level,
        "log_format": log_format.value if log_format is not None else None,
    }
