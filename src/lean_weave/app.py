"""Explicit dependency injection without eager provider construction."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx2
from openai import OpenAI

from lean_weave.config import AppConfig, load_config
from lean_weave.context.provider import DeterministicContextProvider
from lean_weave.contracts import (
    CodeExecutor, ContextProvider, LLMProvider, MetricsCollector,
    StateStore, VerificationRunner, WorkflowOrchestrator,
)
from lean_weave.errors import DependencyUnavailable
from lean_weave.logging_setup import get_logger
from lean_weave.metrics.jsonl import JsonlMetricsCollector
from lean_weave.metrics.models import AttemptJournal
from lean_weave.providers.deepseek import DeepSeekProvider

log = get_logger("app")

_JOURNAL_METHODS = ("reserve_attempt", "finalize_attempt")


@dataclass
class AppContext:
    config: AppConfig
    config_source: Path | None
    llm_provider: LLMProvider | None = None
    context_provider: ContextProvider | None = None
    code_executor: CodeExecutor | None = None
    verification_runner: VerificationRunner | None = None
    state_store: StateStore | None = None
    metrics_collector: MetricsCollector | None = None
    orchestrator: WorkflowOrchestrator | None = None


def build_app_context(
    config_path: Path | None = None, *,
    llm_provider: LLMProvider | None = None,
    context_provider: ContextProvider | None = None,
    code_executor: CodeExecutor | None = None,
    verification_runner: VerificationRunner | None = None,
    state_store: StateStore | None = None,
    metrics_collector: MetricsCollector | None = None,
    orchestrator: WorkflowOrchestrator | None = None,
) -> AppContext:
    loaded = load_config(config_path, required=config_path is not None)
    context = AppContext(
        loaded.config, loaded.source, llm_provider, context_provider, code_executor,
        verification_runner, state_store, metrics_collector, orchestrator,
    )
    names = [
        name for name in (
            "llm_provider", "context_provider", "code_executor", "verification_runner",
            "state_store", "metrics_collector", "orchestrator",
        ) if getattr(context, name) is not None
    ]
    log.info("[AppContext.build] source=%s injected=%s", loaded.source or "defaults", len(names))
    log.debug("[AppContext.build] slots=%s", names)
    log.debug(
        "[AppContext.settings] model=%s timeout_s=%s thinking=%s max_retries=%s max_output_tokens=%s feature_token_budget=%s",
        loaded.config.llm.model,
        loaded.config.llm.timeout_s,
        loaded.config.llm.thinking,
        loaded.config.llm.max_retries,
        loaded.config.llm.max_output_tokens,
        loaded.config.workflow.feature_token_budget,
    )
    return context


def create_context_provider(
    context: AppContext,
    *,
    provider: ContextProvider | None = None,
    metadata_digests: dict[str, str] | None = None,
) -> ContextProvider:
    """Construct the deterministic provider lazily; no scan or Git here.

    An injected provider always wins. Construction only records limits and the
    optional approved metadata digests; repository discovery starts on the
    first ``build`` call.
    """

    if context.context_provider is not None:
        log.debug("[AppContext.context] selection=injected provider")
        return context.context_provider
    if provider is not None:
        return provider
    log.debug(
        "[AppContext.context] selection=deterministic stage_prompt_tokens_max=%s",
        context.config.context.stage_prompt_tokens_max,
    )
    return DeterministicContextProvider(
        Path.cwd(),
        config=context.config,
        metadata_digests=metadata_digests,
    )


def create_llm_provider(    context: AppContext,
    *,
    journal: AttemptJournal | None = None,
    client: OpenAI | None = None,
    transport: httpx2.BaseTransport | None = None,
    api_key: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] | None = None,
) -> LLMProvider:
    """Construct the production provider on demand.

    An injected provider always wins. Otherwise a compatible attempt journal is
    required: an injected journal, an atomic ``metrics_collector`` slot, or the
    default JSONL journal for the configured state directory. A legacy
    collector that cannot reserve atomically fails closed instead of silently
    disabling budgets. No client, key, or journal is opened here.
    """
    if context.llm_provider is not None:
        log.debug("[AppContext.llm] selection=injected provider")
        return context.llm_provider

    accounting = journal if journal is not None else context.metrics_collector
    explicit = accounting is not None
    if accounting is None:
        accounting = JsonlMetricsCollector.from_config(context.config)
    if not all(hasattr(accounting, method) for method in _JOURNAL_METHODS):
        raise DependencyUnavailable(
            "Configured metrics collector cannot reserve token budget atomically"
        )
    log.debug(
        "[AppContext.llm] selection=deepseek journal=%s",
        "injected" if explicit else "jsonl",
    )
    sleep = sleeper if sleeper is not None else time.sleep
    return DeepSeekProvider(
        context.config.llm,
        accounting,  # type: ignore[arg-type]
        feature_token_budget=context.config.workflow.feature_token_budget,
        client=client,
        transport=transport,
        api_key=api_key,
        clock=clock,
        sleeper=sleep,
    )
