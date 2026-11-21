# lean-weave Architecture

## Overview

lean-weave is a **single-process CLI pipeline** that plans, implements, verifies, and retries feature work using:

- structured `FeatureState` (not chat history),
- DeepSeek via `LLMProvider` gateway (`DeepSeekProvider`),
- stage-scoped `ContextProvider` budgets,
- local `CodeExecutor` + deterministic `VerificationRunner`,
- `StateStore` under `.lean-weave/`,
- `MetricsCollector` plus an atomic JSONL usage/budget ledger.

Primary objective: **minimize LLM tokens** while keeping implementation quality.

## Boundaries

```text
CLI → WorkflowOrchestrator
        ├─ ContextProvider (deterministic allowlists)
        ├─ LLMProvider (DeepSeek only in MVP)
        ├─ CodeExecutor (subprocess, cwd jail)
        ├─ VerificationRunner (no LLM in MVP)
        ├─ StateStore
        └─ MetricsCollector
```

MVP exclusions: MCP/ACP, Docker, background workers, multi-agent routers, nested AI Factory agent sessions.

## Token strategy

See `.ai-factory/references/v1-01-token-budget-principles.md`:

- Stage-isolated prompts; zero chat history in FeatureState.
- Digests for artifacts; full files only on allowlist + size caps.
- Never load skill trees or secrets into LLM context.
- Prefer deterministic ops (verify, file select) over LLM calls.

## Orchestrator decision

**Adopt minimal Python state machine** for MVP 1; **defer LangGraph** despite validated interrupt/checkpoint/sqlite resume (spikes). Evidence: `.ai-factory/references/v1-01-orchestrator-comparison.md` · ADR-002.

Methodology reuse from `aif-plan` / `aif-implement` / `aif-verify` is **artifact + procedure** only — no nested autonomous sessions (`.ai-factory/references/v1-01-aif-methodology-reuse.md`).

## Package structure

Proposed layout: `.ai-factory/references/v1-01-project-structure.md`.  
Canonical entry: `lean_weave:main`. `src/main.py` is an IDE sample to remove/rehome later.

Foundation modules: `config/` holds strict YAML models and safe file IO;
`logging_setup.py` owns application stderr logging and secret redaction;
`errors.py` holds the shared application error hierarchy. Configuration paths
and state containment currently use the working directory as the project root.

`app.py` supplies a typed `AppContext` with optional injected Protocol
implementations. `create_llm_provider(context)` is the lazy DeepSeek
construction seam: injected providers win, atomic journals are required, and
nothing dials the network or opens the ledger until a gateway call is made.
`providers/` holds `DeepSeekProvider` and the deterministic `MockLLMProvider`;
`metrics/` holds typed usage records, conservative estimation, and the atomic
JSONL journal. `cli/` is the Typer dispatch/error boundary with offline init,
config inspection, doctor, help-only feature commands, offline `context`
preview, and explicit `llm check`. Both `wl` and `lean-weave` use this single app;
`lean_weave:main` remains the canonical Python entrypoint.

`context/` is the deterministic, LLM-free retrieval seam. `repository.py`
performs bounded path inventory and no-follow content admission; `policy.py`
applies nested ignore rules, hard bans, and a high-confidence credential filter;
`python_index.py` extracts AST symbols and in-repo import edges without importing
modules; `search.py` streams canonical lines to `rg` and falls back to Python;
`git_metadata.py` supplies optional preview-only recency from an isolated Git
subprocess; `ranking.py` ranks inside an eligible scope; `builder.py` admits whole
units under prompt/character caps with provenance and deduplication; `render.py`
produces one system/user message pair; `provider.py` is the stage-scoped adapter.
`app.py::create_context_provider` constructs it lazily without scanning.

`executor/` and `verify/` are standalone code tools. `approval.py` freezes a
trusted root and, only when the caller opts in, a write scope or check
registry. `workspace.py` re-admits paths with no-follow handles, nested ignore
rules, and the shared credential filter; a symlinked or unreadable
`.gitignore` makes that subtree indeterminate. `source.py` serves ranged
reads, whole symbols, and literal symbol search through `python_index.py`.
`patch.py` applies one exact unified diff and replaces the file atomically.
`git_diff.py` returns staged or unstaged path/status metadata from an isolated
read-only Git subprocess. `verify/process.py` captures a fixed argv with a
deadline and byte cap; `verify/checks.py` turns recognized diagnostics into
template summaries. `workflow/tools.py` and `verify/runner.py` bind these
surfaces under fixed names for the orchestrator without widening approvals.

`workflow/` is the MVP 1 minimal deterministic orchestrator. `state.py` owns
contained, atomic, version-checked checkpoints under `.lean-weave/workflow/`
with a single-writer lease and a private control envelope; `results.py`
projects a typed `WorkflowResult`. `lifecycle.py` adds optional review-gate,
continuation, and in-flight/applied-effect fields on the control record.
`session.py` composes worktree provisioning with `create_orchestrator` for the
public CLI, freezes approvals, and reads archived terminal state.
`instructions.py` loads only bounded, registered stage fragments and
`planning.py` validates a `FeaturePlan` before any write. `budget.py` books
conservative reservations before dispatch and settles them against verified
usage; `tool_loop.py` runs a bounded, ephemeral tool loop with cancel checks.
`stages.py` owns transition policy and `orchestrator.py` drives plan →
implement → verify → retry, keeping effect guards until the outcome checkpoint
lands. `app.py::create_orchestrator` is the lazy wiring seam and accepts an
explicit `workspace_root`. Automatic writes require a caller `ToolApproval`,
and checks that execute workspace code require an injected
`IsolationBackend`; there is no direct-execution fallback.

`worktree/` owns the fixed Git adapter, routing registry under the Git common
directory, and `WorktreeManager` lifecycle (create / adopt / inspect /
cleanup). Managed worktrees live in an owner-only sibling parent outside the
source checkout. Routing never stores plans or budgets.

`code_index/` is the optional V2 persistent index. `identity.py` binds a cache
namespace to the resolved checkout path, device, and inode and validates an
owner-only external cache; `models.py` owns canonical facts serialization,
source revisions, policy/extractor keys, and stable identifiers;
`sqlite_store.py` validates the schema, integrity, manifest checksum, scoped
reads, and transactional writes; `index.py` performs strict admission,
incremental reconciliation, checked freshness/lookup, whole-generation reads,
scoped provider loads, and clear. The cache is disposable, contains no source
bodies or Git metadata, and is never consulted for membership, ranking, or
prompt authority. `app.py::create_code_index` is the lazy wiring seam; no cache
path, directory, or database is opened for help, provider construction, plan,
or verify.

`repository_map/` is the optional V2 compact map layer. It reads a checked
`IndexGeneration`, resolves traceable static imports into an in-memory graph,
ranks by direct import evidence, and renders budgeted views. `app.py::create_repository_map`
is lazy. Default ContextProvider behavior is unchanged; only explicit `wl map`
and `wl context build --indexed-map` consume map rows. No LangGraph, graph
database, or orchestrator dependency.

`context/retriever.py` and `context/retrieval_ranking.py` add opt-in task-aware
hybrid ranking over one strict text scan, with optional checked-graph and Git
path signals injected at `app.py::create_retrieval_provider`. Stage
`ContextProvider.build` is unchanged; only explicit `retrieve()` / CLI preview
paths consume ranked hits.

## ADRs

| ADR | Topic |
|-----|-------|
| ADR-001 | Component boundaries |
| ADR-002 | Orchestrator LangGraph vs minimal |
| ADR-003 | LLM provider DeepSeek |
| ADR-004 | Context isolation and state |

## Related references

- Contracts, schemas, isolation, risks, MVP acceptance under `.ai-factory/references/v1-01-*.md`
- Spikes under `spikes/`
