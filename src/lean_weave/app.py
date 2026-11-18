"""Explicit dependency injection without provider construction or side effects."""

from dataclasses import dataclass
from pathlib import Path

from lean_weave.config import AppConfig, load_config
from lean_weave.contracts import (
    CodeExecutor, ContextProvider, LLMProvider, MetricsCollector,
    StateStore, VerificationRunner, WorkflowOrchestrator,
)
from lean_weave.logging_setup import get_logger

log = get_logger("app")


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
    return context
