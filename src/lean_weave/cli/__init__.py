"""Typer application and console error boundary."""

from collections.abc import Sequence

import click
import typer

from lean_weave.cli.app import app
from lean_weave.errors import ExitCode, LeanWeaveError
from lean_weave.logging_setup import redact_console_message


def main(argv: Sequence[str] | None = None) -> None:
    code = ExitCode.OK
    try:
        result = app(args=list(argv) if argv is not None else None, standalone_mode=False)
        if isinstance(result, int):
            code = result
    except LeanWeaveError as exc:
        typer.echo(f"Error: {redact_console_message(str(exc))}", err=True)
        code = exc.exit_code
    except click.ClickException as exc:
        # Parser input is untrusted and may itself contain a secret.
        message = redact_console_message(exc.format_message())
        typer.echo(f"Error: {message}", err=True)
        code = ExitCode.USAGE if isinstance(exc, click.UsageError) else ExitCode.ERROR
    raise SystemExit(code)


__all__ = ["app", "main"]
