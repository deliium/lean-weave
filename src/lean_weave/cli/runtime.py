"""Lazy config-consuming command setup; never called during help parsing."""

import io
import logging
import os

import typer

from lean_weave.app import AppContext, build_app_context
from lean_weave.errors import LeanWeaveError
from lean_weave.logging_setup import configure_logging, get_logger

log = get_logger("cli")


def _replay_diagnostics(buffer: io.StringIO) -> None:
    """Replay already-redacted load messages under the effective level/format."""
    for line in buffer.getvalue().splitlines():
        parts = line.split(" ", 2)
        if len(parts) != 3:
            continue
        level, name, message = parts
        if name in {"lean_weave", "lean_weave.logging"}:
            continue  # Final configuration emits its own authoritative diagnostic.
        get_logger(name).log(logging.getLevelNamesMapping().get(level, logging.INFO), message)


def command_context(ctx: typer.Context) -> AppContext:
    root = ctx.find_root()
    if isinstance(root.obj, AppContext):
        return root.obj
    options = root.obj
    diagnostics = io.StringIO()
    configure_logging("DEBUG", stream=diagnostics)
    try:
        context = build_app_context(options["config_path"])
    except LeanWeaveError:
        configure_logging(options["log_level"], options["log_format"] or "text")
        _replay_diagnostics(diagnostics)
        raise
    config = context.config
    selected = options["log_level"]
    if selected is None:
        selected = os.getenv("LEAN_WEAVE_LOG_LEVEL") or os.getenv("LOG_LEVEL") or config.logging.level
    configure_logging(
        selected, options["log_format"] or config.logging.format,
        secret_env_names=("DEEPSEEK_API_KEY", config.llm.api_key_env),
    )
    _replay_diagnostics(diagnostics)
    if selected != config.logging.level:
        log.warning("[CLI.run] logging environment/flag override differs from file")
    log.info("[CLI.run] command=%s source=%s", ctx.command.name, context.config_source or "defaults")
    root.obj = context
    ctx.obj = context
    return context
