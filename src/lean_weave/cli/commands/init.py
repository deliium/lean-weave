import typer

from lean_weave.config import init_config
from lean_weave.logging_setup import configure_logging, get_logger


def initialize(ctx: typer.Context, force: bool = typer.Option(False, "--force")) -> None:
    """Create a commented product configuration file."""
    options = ctx.find_root().obj
    configure_logging(options["log_level"], options["log_format"] or "text")
    typer.echo(str(init_config(options["config_path"], force=force)))
    get_logger("cli").info("[CLI.init] exit_code=0")
