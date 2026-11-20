"""Explicit dependency injection without eager provider construction."""

import time
from collections.abc import Callable, Mapping, Sequence
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
from lean_weave.executor.approval import ToolApproval
from lean_weave.logging_setup import get_logger
from lean_weave.metrics.jsonl import JsonlMetricsCollector
from lean_weave.metrics.models import AttemptJournal
from lean_weave.providers.deepseek import DeepSeekProvider
from lean_weave.verify.process import IsolationBackend
from lean_weave.verify.runner import ApprovedChecksRunner
from lean_weave.workflow.instructions import load_instructions
from lean_weave.workflow.orchestrator import MinimalWorkflowOrchestrator
from lean_weave.workflow.planning import PlanPolicy
from lean_weave.workflow.state import JsonCheckpointStore, checkpoint_capability
from lean_weave.workflow.tools import WorkflowTools

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


def create_orchestrator(
    context: AppContext,
    *,
    approval: ToolApproval | None = None,
    backend: IsolationBackend | None = None,
    check_ids: Sequence[str] | None = None,
    verification_runner: VerificationRunner | None = None,
    tools: WorkflowTools | None = None,
    plan_policy: PlanPolicy | None = None,
    journal: AttemptJournal | None = None,
    model: str | None = None,
    thinking: str | None = None,
    instructions: Mapping[str, Sequence[str]] | None = None,
) -> WorkflowOrchestrator:
    """Construct the minimal driver lazily; no network, scan, or ledger I/O.

    An injected orchestrator always wins. Otherwise the existing injected
    dependencies and provider factories are reused. Automatic writes require an
    explicit ``ToolApproval``; no approval means no patch authority.
    """

    if context.orchestrator is not None:
        log.debug("[AppContext.orchestrator] selection=injected")
        return context.orchestrator

    store = context.state_store
    if store is None:
        store = JsonCheckpointStore(
            Path.cwd(), state_dir=context.config.workflow.state_dir
        )
    if not checkpoint_capability(store):
        raise DependencyUnavailable("StateStore cannot provide atomic workflow checkpoints")
    store_root = getattr(store, "root", None)
    if approval is not None and store_root is not None and Path(store_root) != approval.root:
        raise DependencyUnavailable("State store and tool approval must share a workspace root")

    if verification_runner is None:
        if approval is None or check_ids is None:
            raise DependencyUnavailable(
                "An approved check registry is required for verification"
            )
        verification_runner = ApprovedChecksRunner(
            approval, check_ids=check_ids, backend=backend
        )
    if tools is None and approval is not None:
        tools = WorkflowTools(approval, task_files=[], checks_ready=True)
    if plan_policy is None and approval is not None:
        plan_policy = PlanPolicy(
            approved_paths=frozenset(approval.write_allow),
            approved_check_ids=frozenset(check_ids or ()),
            bypass_allowed=True,
            require_bypass_checks=True,
        )

    selected_instructions = dict(instructions or {})
    if not selected_instructions:
        selected_instructions = {
            stage: load_instructions(stage) for stage in ("plan", "implement", "verify", "retry")
        }
    accounting = journal if journal is not None else context.metrics_collector
    if accounting is None and context.llm_provider is None:
        # One ledger instance backs both gateway admission and workflow
        # feature-wide spend, so the two layers cannot disagree.
        accounting = JsonlMetricsCollector.from_config(context.config)
    orchestrator = MinimalWorkflowOrchestrator(
        config=context.config.workflow,
        llm_provider=create_llm_provider(context, journal=accounting),  # type: ignore[arg-type]
        context_provider=create_context_provider(context),
        state_store=store,
        verification_runner=verification_runner,
        tools=tools,
        plan_policy=plan_policy,
        model=model if model is not None else context.config.llm.model,
        thinking=(
            thinking
            if thinking is not None
            else ("enabled" if context.config.llm.thinking else "disabled")
        ),
        output_cap=context.config.llm.max_output_tokens,
        instructions=selected_instructions,
        journal=accounting,
    )
    log.info(
        "[AppContext.orchestrator] selection=minimal writes=%s checks=%s ledger=%s",
        tools is not None and tools.approval.writes_approved,
        check_ids is not None,
        accounting is not None,
    )
    return orchestrator
