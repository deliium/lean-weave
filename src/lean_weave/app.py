"""Explicit dependency injection without eager provider construction."""

import contextlib
import os
import stat
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx2
from openai import OpenAI

from lean_weave.code_index import CodeIndex
from lean_weave.config import AppConfig, load_config
from lean_weave.context.git_metadata import read_retrieval_diff
from lean_weave.context.packer import ContextPacker
from lean_weave.context.provider import DeterministicContextProvider
from lean_weave.context.repository import Repository, validate_index_source_path
from lean_weave.context.retriever import ContextRetriever
from lean_weave.context_cache import DisabledContextCache, SqliteContextCache
from lean_weave.context_cache.facts import CachedFactLoader
from lean_weave.context_cache.models import ContextCache
from lean_weave.context_cache.peer import AuthorizedFactPeer
from lean_weave.context_cache.service import CachePolicy
from lean_weave.errors import WorktreeError
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
from lean_weave.repository_map import MapLimits, RepositoryMap
from lean_weave.verify.process import IsolationBackend, run_bounded
from lean_weave.verify.runner import ApprovedChecksRunner
from lean_weave.workflow.instructions import load_instructions
from lean_weave.workflow.orchestrator import MinimalWorkflowOrchestrator
from lean_weave.workflow.planning import PlanPolicy
from lean_weave.workflow.state import JsonCheckpointStore, checkpoint_capability
from lean_weave.workflow.tools import WorkflowTools
from lean_weave.worktree.manager import WorktreeManager
from lean_weave.worktree.registry import Ownership

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


def _protected_index_paths(context: AppContext, root: Path) -> tuple[str, ...]:
    source = context.config_source
    if source is None:
        return ()
    try:
        resolved = Path(source).resolve()
        if not resolved.is_relative_to(root):
            return ()
        return (resolved.relative_to(root).as_posix(),)
    except (OSError, RuntimeError, ValueError):
        return ()


def create_code_index(
    context: AppContext,
    *,
    workspace_root: Path | None = None,
    cache_base: Path | None = None,
) -> CodeIndex:
    """Construct the lazy persistent-index seam; no scan, cache, or DB I/O."""

    root = Path.cwd() if workspace_root is None else Path(workspace_root)
    try:
        resolved_root = root.resolve()
    except (OSError, RuntimeError, ValueError):
        resolved_root = root
    return CodeIndex(
        root,
        context_config=context.config.context,
        state_dir=context.config.workflow.state_dir,
        protected_paths=_protected_index_paths(context, resolved_root),
        cache_base=cache_base,
    )


def create_context_cache(
    context: AppContext,
    *,
    workspace_root: Path | None = None,
    cache_base: Path | None = None,
    force: bool = False,
) -> ContextCache:
    """Construct the lazy cache seam; no scan, directory, or database open.

    ``force`` is reserved for explicit offline maintenance (confirmed clear).
    """

    root = Path.cwd() if workspace_root is None else Path(workspace_root)
    try:
        resolved_root = root.resolve()
    except (OSError, RuntimeError, ValueError):
        resolved_root = root
    settings = context.config.context.cache
    if not settings.enabled and not force:
        return DisabledContextCache()
    return SqliteContextCache(
        root,
        config=settings,
        context_config=context.config.context,
        state_dir=context.config.workflow.state_dir,
        protected_paths=_protected_index_paths(context, resolved_root),
        cache_base=cache_base,
    )


@contextlib.contextmanager
def open_peer_fact_reuse(
    manager: WorktreeManager,
    *,
    local_feature_id: str,
    peer_feature_id: str,
    local_context: AppContext,
    peer_context: AppContext | None = None,
    allowed_paths: Iterable[str],
    cache_base: Path | None = None,
):
    """Yield a short-lived capability for trusted same-project Python facts.

    Both feature IDs must resolve to ready managed worktrees of one repository
    with matching admission/analysis configuration.  Empty scope disables reuse.
    """

    peer_context = peer_context if peer_context is not None else local_context
    local = manager.revalidate(local_feature_id)
    peer = manager.revalidate(peer_feature_id)
    if (
        local.record.ownership is not Ownership.MANAGED
        or peer.record.ownership is not Ownership.MANAGED
    ):
        raise WorktreeError("peer_reuse_requires_managed_worktrees")
    local_root = Path(local.identity.root)
    peer_root = Path(peer.identity.root)
    if local_root.resolve() == peer_root.resolve():
        raise WorktreeError("peer_reuse_requires_distinct_checkouts")
    try:
        local_common = local.identity.common_dir.resolve()
        peer_common = peer.identity.common_dir.resolve()
        local_stat = os.stat(local_common, follow_symlinks=False)
        peer_stat = os.stat(peer_common, follow_symlinks=False)
    except OSError:
        raise WorktreeError("peer_reuse_unavailable") from None
    if local_common != peer_common or (
        local_stat.st_dev,
        local_stat.st_ino,
    ) != (peer_stat.st_dev, peer_stat.st_ino):
        raise WorktreeError("peer_reuse_requires_same_repository")
    for root in (local_root, peer_root):
        try:
            info = os.stat(root, follow_symlinks=False)
        except OSError:
            raise WorktreeError("peer_reuse_unavailable") from None
        if info.st_uid not in {0, os.geteuid()} or not stat.S_ISDIR(info.st_mode):
            raise WorktreeError("peer_reuse_unavailable")
        if not os.access(root, os.R_OK | os.X_OK):
            raise WorktreeError("peer_reuse_unavailable")

    local_settings = local_context.config.context.cache
    peer_settings = peer_context.config.context.cache
    if not local_settings.enabled or not peer_settings.enabled:
        raise WorktreeError("peer_reuse_requires_cache")
    local_policy = CachePolicy.from_context(
        local_context.config.context,
        state_dir=local_context.config.workflow.state_dir,
        protected_paths=_protected_index_paths(local_context, local_root),
    )
    peer_policy = CachePolicy.from_context(
        peer_context.config.context,
        state_dir=peer_context.config.workflow.state_dir,
        protected_paths=_protected_index_paths(peer_context, peer_root),
    )
    if local_policy.digest() != peer_policy.digest():
        raise WorktreeError("peer_reuse_configuration_mismatch")

    scope: list[str] = []
    for value in allowed_paths:
        scope.append(validate_index_source_path(value))
    bounded = frozenset(scope)
    if not bounded:
        raise WorktreeError("peer_reuse_requires_scope")

    local_repository = Repository(
        local_root,
        local_context.config.context,
        state_dir=local_context.config.workflow.state_dir,
        strict_source=True,
        protected_paths=_protected_index_paths(local_context, local_root),
    )
    peer_repository = Repository(
        peer_root,
        peer_context.config.context,
        state_dir=peer_context.config.workflow.state_dir,
        strict_source=True,
        protected_paths=_protected_index_paths(peer_context, peer_root),
    )
    local_cm = local_repository.scan_python_sources()
    peer_cm = peer_repository.scan_python_sources()
    local_scan = local_cm.__enter__()
    peer_scan = None
    capability = None
    try:
        peer_scan = peer_cm.__enter__()
        if not local_scan.complete or not peer_scan.complete:
            raise WorktreeError("peer_reuse_incomplete_scan")
        local_cache = SqliteContextCache(
            local_root,
            config=local_settings,
            context_config=local_context.config.context,
            state_dir=local_context.config.workflow.state_dir,
            protected_paths=_protected_index_paths(local_context, local_root),
            cache_base=cache_base,
        )
        peer_cache = SqliteContextCache(
            peer_root,
            config=peer_settings,
            context_config=peer_context.config.context,
            state_dir=peer_context.config.workflow.state_dir,
            protected_paths=_protected_index_paths(peer_context, peer_root),
            cache_base=cache_base,
        )
        capability = AuthorizedFactPeer(
            local_cache=local_cache,
            peer_cache=peer_cache,
            local_scan=local_scan,
            peer_scan=peer_scan,
            allowed_paths=bounded,
            policy=local_policy,
        )
        yield capability
    finally:
        if capability is not None:
            capability.close()
        if peer_scan is not None:
            peer_cm.__exit__(None, None, None)
        local_cm.__exit__(None, None, None)


def create_repository_map(
    context: AppContext,
    *,
    workspace_root: Path | None = None,
    cache_base: Path | None = None,
) -> RepositoryMap:
    """Construct the lazy repository-map seam; no scan or database open."""

    index = create_code_index(
        context, workspace_root=workspace_root, cache_base=cache_base
    )
    limits = MapLimits(
        tokens_max=context.config.context.map_tokens_max,
        rows_max=context.config.context.map_rows_max,
    )
    cache = create_context_cache(
        context, workspace_root=workspace_root, cache_base=cache_base
    )
    log.debug(
        "[AppContext.repository_map] selection=lazy tokens_max=%s rows_max=%s cache=%s",
        limits.tokens_max,
        limits.rows_max,
        cache.enabled,
    )
    return RepositoryMap(index, limits=limits, cache=cache)


def create_indexed_map_preview_pair(
    context: AppContext,
    *,
    workspace_root: Path | None = None,
    cache_base: Path | None = None,
) -> tuple[Repository, RepositoryMap]:
    """Build a matching Repository + RepositoryMap pair for opt-in context preview."""

    root = Path.cwd() if workspace_root is None else Path(workspace_root)
    try:
        resolved_root = root.resolve()
    except (OSError, RuntimeError, ValueError):
        resolved_root = root
    protected = _protected_index_paths(context, resolved_root)
    repository = Repository(
        root,
        context.config.context,
        state_dir=context.config.workflow.state_dir,
        protected_paths=protected,
    )
    repository_map = create_repository_map(
        context, workspace_root=root, cache_base=cache_base
    )
    return repository, repository_map


def _fact_loader_factory(context: AppContext, *, root: Path):
    cache = create_context_cache(context, workspace_root=root)
    index = create_code_index(context, workspace_root=root)
    if cache.enabled:
        resolved = root.resolve()
        repository = Repository(
            root,
            context.config.context,
            state_dir=context.config.workflow.state_dir,
            strict_source=True,
            protected_paths=_protected_index_paths(context, resolved),
        )
        return lambda: CachedFactLoader(
            cache,
            repository,
            policy=cache.policy,  # type: ignore[attr-defined]
            index=index,
        )
    return lambda: index.fact_loader()


def _deterministic_context_provider(
    context: AppContext,
    *,
    root: Path,
    metadata_digests: dict[str, str] | None = None,
    retriever_factory=None,
) -> DeterministicContextProvider:
    return DeterministicContextProvider(
        root,
        config=context.config,
        metadata_digests=metadata_digests,
        fact_loader_factory=_fact_loader_factory(context, root=root),
        retriever_factory=retriever_factory,
    )


def create_context_provider(
    context: AppContext,
    *,
    provider: ContextProvider | None = None,
    metadata_digests: dict[str, str] | None = None,
    workspace_root: Path | None = None,
) -> ContextProvider:
    """Construct the deterministic provider lazily; no scan or Git here.

    An injected provider always wins. Construction only records limits and the
    optional approved metadata digests; repository discovery starts on the
    first ``build`` call. ``workspace_root`` defaults to the process cwd for
    existing callers; the feature session passes an explicit worktree root.
    """

    if context.context_provider is not None:
        log.debug("[AppContext.context] selection=injected provider")
        return context.context_provider
    if provider is not None:
        return provider
    root = Path.cwd() if workspace_root is None else workspace_root
    log.debug(
        "[AppContext.context] selection=deterministic stage_prompt_tokens_max=%s",
        context.config.context.stage_prompt_tokens_max,
    )
    return _deterministic_context_provider(
        context, root=root, metadata_digests=metadata_digests
    )


def create_retrieval_provider(
    context: AppContext,
    *,
    workspace_root: Path | None = None,
    cache_base: Path | None = None,
    authorized_fact_peer: AuthorizedFactPeer | None = None,
) -> DeterministicContextProvider:
    """Build the concrete preview provider with lazy graph/diff retrieval wiring.

    Always returns the built-in deterministic adapter for offline CLI preview.
    Injected stage providers are not selected here.
    """

    root = Path.cwd() if workspace_root is None else Path(workspace_root)
    try:
        resolved_root = root.resolve()
    except (OSError, RuntimeError, ValueError):
        resolved_root = root
    protected = _protected_index_paths(context, resolved_root)
    repository_map = create_repository_map(
        context, workspace_root=root, cache_base=cache_base
    )
    cache = create_context_cache(
        context, workspace_root=root, cache_base=cache_base
    )

    def factory() -> ContextRetriever:
        repository = Repository(
            root,
            context.config.context,
            state_dir=context.config.workflow.state_dir,
            strict_source=True,
            protected_paths=protected,
        )

        def load_graph(scan):
            return repository_map.graph_for_retrieval_scan(scan)

        def read_diff(diff_root, admitted_paths):
            return read_retrieval_diff(
                diff_root,
                admitted_paths,
                run_capture=run_bounded,
            )

        log.debug("[AppContext.retrieval] selection=lazy_retriever")
        return ContextRetriever(
            repository,
            load_graph=load_graph,
            read_diff=read_diff,
            cache=cache,
            authorized_fact_peer=authorized_fact_peer,
        )

    log.debug("[AppContext.retrieval] selection=deterministic_preview")
    return _deterministic_context_provider(
        context, root=root, retriever_factory=factory
    )


def create_context_packer(
    context: AppContext,
    *,
    workspace_root: Path | None = None,
    cache_base: Path | None = None,
) -> ContextPacker:
    """Construct the lazy V2 packer; no scan, cache, ledger or network I/O."""

    root = Path.cwd() if workspace_root is None else Path(workspace_root)
    try:
        resolved_root = root.resolve()
    except (OSError, RuntimeError, ValueError):
        resolved_root = root
    protected = _protected_index_paths(context, resolved_root)
    repository_map = create_repository_map(
        context, workspace_root=root, cache_base=cache_base
    )
    repository = Repository(
        root,
        context.config.context,
        state_dir=context.config.workflow.state_dir,
        strict_source=True,
        protected_paths=protected,
    )

    def load_graph(scan):
        return repository_map.graph_for_retrieval_scan(scan)

    cache = create_context_cache(
        context, workspace_root=workspace_root, cache_base=cache_base
    )
    retriever = ContextRetriever(repository, load_graph=load_graph, cache=cache)
    log.debug(
        "[AppContext.packing] selection=lazy model=%s total_tokens=%s cli_default=%s",
        context.config.llm.model,
        context.config.context.packing.total_tokens,
        context.config.context.packing.cli_default,
    )
    return ContextPacker(
        repository,
        retriever=retriever,
        model=context.config.llm.model,
        thinking=context.config.llm.thinking,
        cache=cache,
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
    workspace_root: Path | None = None,
    orchestrator_kwargs: Mapping[str, object] | None = None,
) -> WorkflowOrchestrator:
    """Construct the minimal driver lazily; no network, scan, or ledger I/O.

    An injected orchestrator always wins. Otherwise the existing injected
    dependencies and provider factories are reused. Automatic writes require an
    explicit ``ToolApproval``; no approval means no patch authority.
    ``workspace_root`` defaults to cwd; the feature session passes the worktree.
    """

    if context.orchestrator is not None:
        log.debug("[AppContext.orchestrator] selection=injected")
        return context.orchestrator

    root = Path.cwd() if workspace_root is None else workspace_root
    store = context.state_store
    if store is None:
        store = JsonCheckpointStore(
            root, state_dir=context.config.workflow.state_dir
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

    # Empty map → each stage loads its registered defaults by ID.
    # Do not preload fragment text here; load_instructions treats values as IDs.
    selected_instructions = dict(instructions or {})
    accounting = journal if journal is not None else context.metrics_collector
    if accounting is None and context.llm_provider is None:
        # One ledger instance backs both gateway admission and workflow
        # feature-wide spend, so the two layers cannot disagree.
        state_path = (root / context.config.workflow.state_dir).resolve()
        accounting = JsonlMetricsCollector(state_path)
    extra = dict(orchestrator_kwargs or {})
    orchestrator = MinimalWorkflowOrchestrator(
        config=context.config.workflow,
        llm_provider=create_llm_provider(context, journal=accounting),  # type: ignore[arg-type]
        context_provider=create_context_provider(context, workspace_root=root),
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
        **extra,  # type: ignore[arg-type]
    )
    log.info(
        "[AppContext.orchestrator] selection=minimal writes=%s checks=%s ledger=%s",
        tools is not None and tools.approval.writes_approved,
        check_ids is not None,
        accounting is not None,
    )
    return orchestrator
