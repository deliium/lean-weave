import os
import shutil
import sys
import tempfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import typer

from lean_weave import __version__
from lean_weave.config.loader import resolve_state_dir
from lean_weave.errors import LeanWeaveError
from lean_weave.logging_setup import get_logger
from lean_weave.cli.runtime import command_context

log = get_logger("cli")


def check_writable(path: Path) -> bool:
    missing = not path.exists()
    parent = path
    while not parent.exists():
        parent = parent.parent
    if not parent.is_dir():
        raise LeanWeaveError("State directory target or existing parent is not a directory")
    probe = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".lean-weave-doctor-", dir=parent, delete=False) as stream:
            probe = Path(stream.name)
            stream.write(b"")
    except OSError:
        raise LeanWeaveError("State directory or existing parent is not writable") from None
    finally:
        if probe is not None:
            try:
                probe.unlink()
            except OSError:
                raise LeanWeaveError("Unable to remove doctor writability probe") from None
    return missing


def doctor(ctx: typer.Context) -> None:
    """Check local prerequisites without network access or directory creation."""
    context = command_context(ctx)
    if sys.version_info < (3, 12):
        raise LeanWeaveError("Python >= 3.12 is required")
    try:
        package_version = version("lean-weave")
    except PackageNotFoundError:
        package_version = __version__
    typer.echo(f"OK Python {sys.version_info.major}.{sys.version_info.minor}")
    typer.echo(f"OK lean-weave {package_version}")
    path = resolve_state_dir(context.config)
    missing = check_writable(path)
    typer.echo(f"{'WARN absent; parent writable' if missing else 'OK writable'} state directory: {path}")
    if missing:
        log.warning("[CLI.doctor] state directory absent; existing parent writable")
    git_present = shutil.which("git") is not None
    key_present = bool(os.getenv(context.config.llm.api_key_env))
    typer.echo(f"{'OK' if git_present else 'WARN'} git {'present' if git_present else 'absent (optional)'}")
    typer.echo(f"{'OK' if key_present else 'WARN'} API key {'present' if key_present else 'absent (optional)'}")
    if not git_present:
        log.warning("[CLI.doctor] git absent (optional)")
    if not key_present:
        log.warning("[CLI.doctor] configured secret environment variable absent (optional)")
    log.debug("[CLI.doctor] checks=python,package,config,state,git,key")
    log.info("[CLI.doctor] exit_code=0")
