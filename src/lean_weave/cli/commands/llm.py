"""Explicit, opt-in DeepSeek connection check."""

from __future__ import annotations

import re
import time

import typer

from lean_weave.app import create_llm_provider
from lean_weave.cli.runtime import command_context
from lean_weave.contracts._types import PROBE_FEATURE_ID, PROBE_STAGE
from lean_weave.logging_setup import (
    SecretRedactionFilter,
    get_logger,
    redact_console_message,
)

log = get_logger("cli")

llm_app = typer.Typer(help="LLM provider checks (explicit network access).", no_args_is_help=True)
_UNSAFE_TEXT = re.compile(r"[^\x20-\x7e]")


def _safe_text(value: object, fallback: str, secret_env_names: tuple[str, ...]) -> str:
    text = SecretRedactionFilter(secret_env_names).redact(
        redact_console_message(str(value))
    )
    text = _UNSAFE_TEXT.sub("", text).strip()
    return text[:128] or fallback


@llm_app.command("check")
def check(ctx: typer.Context) -> None:
    """Send one small fixed DeepSeek request and report safe usage metadata."""
    context = command_context(ctx)
    created = context.llm_provider is None
    provider = create_llm_provider(context)
    started = time.monotonic()
    try:
        response = provider.complete(
            {
                "feature_id": PROBE_FEATURE_ID,
                "stage": PROBE_STAGE,
                "messages": [{"role": "user", "content": "Reply with the single word OK."}],
                "thinking": "disabled",
                "max_output_tokens": min(context.config.llm.max_output_tokens, 32),
                "options": {"retry_limit": 0, "thinking": "disabled"},
            }
        )
    finally:
        if created:
            close = getattr(provider, "close", None)
            if callable(close):
                close()

    model = _safe_text(
        response.get("model") or context.config.llm.model,
        "unknown",
        ("DEEPSEEK_API_KEY", context.config.llm.api_key_env),
    )
    duration_ms = max(0, int((time.monotonic() - started) * 1000))
    usage = response.get("usage")
    if not response.get("usage_reported") or usage is None:
        typer.echo(f"OK model={model} usage=unavailable")
    else:
        typer.echo(
            "OK model=%s input_tokens=%s output_tokens=%s total_tokens=%s"
            " cache_hit_tokens=%s reasoning_tokens=%s"
            % (
                model,
                usage.prompt_tokens,
                usage.completion_tokens,
                usage.total_tokens,
                usage.prompt_cache_hit_tokens,
                usage.reasoning_tokens,
            )
        )
    log.info("[CLI.llm.check] status=ok model=%s duration_ms=%s", model, duration_ms)
