[← Getting Started](getting-started.md) · [Back to README](../README.md) · [CLI Reference →](cli.md)

# Architecture

lean-weave is a **single-process** coding pipeline: plan → implement → verify → retry while minimizing LLM tokens.

Authoritative agent docs: [ARCHITECTURE.md](../.ai-factory/ARCHITECTURE.md). This page is the human overview.

## Design choices

- **Structured state**, not chat logs — `FeatureState`, `FeaturePlan`, tasks, `VerificationResult`
- **DeepSeek** as the MVP LLM (OpenAI-compatible API) with explicit thinking / tools / `reasoning_content` handling
- **Budgeted gateway** — each outbound attempt reserves estimated input plus output cap in an atomic JSONL ledger; measured usage and local estimates stay separate
- **Minimal state machine** orchestrator for MVP 1; LangGraph interrupt/checkpoint capabilities were validated in spikes and **deferred**
- **Deterministic verification** (compile / tests / schema / file checks) preferred over LLM review
- **Stage-scoped context** with hard bans on secrets, full skill trees, and cross-stage transcripts
- **Deterministic context retrieval** — bounded repository inventory, AST symbols/imports, literal search with an `rg`/Python fallback, lexicographic ranking, and prompt budgets; no embeddings, LLM calls, or persistent index
- **Standalone code tools** — exact reads, one-file patches, approved checks, and read-only Git metadata through an explicit caller approval snapshot
- **Worktree session** — public CLI provisions or adopts a linked worktree, freezes approvals, and drives the existing orchestrator without a second engine

## Component boundaries

```text
CLI → FeatureSession → WorktreeManager + WorkflowOrchestrator
                              ├─ ContextProvider
                              ├─ LLMProvider
                              ├─ CodeExecutor / VerificationRunner
                              ├─ StateStore (per worktree)
                              └─ MetricsCollector (per worktree journal)
```

Contracts (typing-only Protocols): `src/lean_weave/contracts/`.  
Schemas (Pydantic): `src/lean_weave/schemas/`.

## Context retrieval

`src/lean_weave/context/` implements the deterministic `ContextProvider` seam.
It is read-only and LLM-free: no index, pack, transcript, or usage record is
persisted.

```text
Repository (inventory + safe admissions)
  → policy (ignore rules, hard bans, credential filter)
  → python_index (AST symbols + in-repo import edges)
  → repository_map (compact structural map)
  → search (rg stdin / Python fallback) + git_metadata (preview recency)
  → ranking (exact symbol → explicit path → terms → one-hop → path → recency)
  → builder (progressive admission, dedup, provenance, budgets)
  → render (one system + one user message)
  → provider (stage adapter)
```

Design constraints:

- Inventory is path-only; bounded snapshots are admitted through one policy for
  maps, search, AST indexing, and rendering. Preview indexing/search may read
  eligible candidates before prompt admission; workflow stages prefilter to
  their approved scope first.
- Ranking stays inside an eligible stage scope; it never widens it. Stage
  builds also scope the repository map to task paths, one-hop imports, and
  enabled matching tests.
- Credential-quarantined candidates and secret-like path names are excluded
  from maps, packs, prompts, and CLI output before exposure.
- The root is the canonical working directory, not the config file parent or Git
  top level.
- Git and `rg` are optional; both degrade safely. Git never runs for workflow
  stages.
- Prompt estimates are local and conservative. They are separate from measured
  provider usage and the gateway's per-feature spend ledger.
- Plan and verify use approved metadata digests only. Implement reads task paths,
  one-hop imports, and enabled matching tests. Retry reads eligible affected
  files and ignores arbitrary diagnostics.
- Workflows are not wired to the provider in v1-04; `wl context` is an offline
  preview surface.

## Standalone code tools

`src/lean_weave/executor/` and `src/lean_weave/verify/` read, patch, and check
one explicit workspace. YAML is data: writes and check argv are copied into an
immutable approval only when the caller opts in. There is no tool CLI command.
`workflow/` binds these functions under fixed names; it does not widen the
approval snapshot.

```text
approve_tools (explicit root, optional write scope, optional check registry)
  → workspace (no-follow admission, ignore policy, credential filter)
  → source (read_range, read_symbol, search_symbols)
  → patch (exact unified diff, atomic replace)
  → git_diff (staged or unstaged path/status only)
  → verify (approved argv, bounded capture, template diagnostics)
```

Reads reuse the context AST index and hard-deny rules. A bad `.gitignore` makes
its subtree indeterminate instead of being ignored. Patches require the current
byte revision and do not create, delete, or rename files. Git inspection is
read-only and does not search a parent repository. Checks that load workspace
code need an injected offline backend; none is bundled here.

Details, request shapes, and limits: [Standalone code tools](code-tools.md).

## Workflow orchestrator and durable state

`src/lean_weave/workflow/` implements the minimal deterministic state machine.
There is no graph framework runtime dependency.

```text
MinimalWorkflowOrchestrator
  run_feature  ── ContextProvider.build("plan") → validated FeaturePlan
  run_simple   ── caller-supplied single-task spec (no planning call)
  resume       ── checkpoint + fresh approvals, never prior messages
  result       ── read-only WorkflowResult projection
```

Stage flow: `PLAN → IMPLEMENT (one ready task) → VERIFY → DONE | RETRY → IMPLEMENT | FAILED`.
Retries are cumulative and bounded. Successful unrelated tasks are not rerun.
Budget exhaustion is projected as `budget_exhausted` while the public
`FeatureState.stage` remains `failed`.

Durable state lives under `.lean-weave/workflow/`. Each feature has one atomic,
version-checked checkpoint containing the schema v1.0.0 `FeatureState` plus a
private control envelope (task/plan spend, pending effect marker, terminal
reason). A single-writer lease rejects concurrent writers, symlinks, torn files,
oversized records, duplicate starts, and workspace mismatches. Schema v1.0.0
payloads without an envelope still decode.

Entry points:

- `create_orchestrator(AppContext, approval=..., check_ids=..., backend=...)` in
  `app.py` is the lazy wiring seam. It reuses injected dependencies and provider
  factories; construction performs no network call, repository scan, or ledger
  write.
- `MinimalWorkflowOrchestrator.run_feature(...)` is the string-in/state-out
  protocol path.
- `run_simple(feature_id, request, task_spec)` is the typed bypass; invalid
  input fails before any planning call or write.
- `result(feature_id)` returns `success`, `failed`, `budget_exhausted`, or
  `interrupted` with a reason code, task status, verification summary, and
  cumulative counters.

Authority and limits:

- Caller approval supplies authority. Plan text, YAML, tool responses, and
  repository content never widen it.
- Patches are limited to the active task's approved files and the current
  byte revision. A committed patch whose durability is unconfirmed stops the
  batch, preserves the effect marker, and interrupts without replay.
- Checks that execute workspace code require an injected offline
  `IsolationBackend`; without it the workflow refuses before editing. There is
  no direct-execution fallback.
- Limits cover plan calls/tokens, per-task calls/tokens, tool rounds and batch
  size, retries, and the feature token budget. A reservation is booked before
  dispatch and settled from verified usage, or retained conservatively.

## Orchestrator decision

| Option | MVP 1 |
|--------|-------|
| Minimal Python state machine | **Adopted** |
| LangGraph | Deferred (spikes retained) |

Evidence: [orchestrator comparison](../.ai-factory/references/v1-01-orchestrator-comparison.md) · [ADR-002](../.ai-factory/adr/ADR-002-orchestrator-langgraph-vs-minimal.md).

Methodology from `aif-plan` / `aif-implement` / `aif-verify` is reused as **artifacts + procedures**, not nested agent sessions.

## Package layout (target)

```text
src/lean_weave/   # cli, config, context, executor, verify, workflow, providers, metrics, contracts, schemas, app
spikes/           # bounded probes — not production CLI
tests/            # config / contracts / providers / metrics / cli / context / executor / verify / workflow
.lean-weave/      # runtime FeatureState and metrics ledger (gitignored)
.ai-factory/      # DESCRIPTION, ARCHITECTURE, plans, ADRs, references
docs/             # human docs
```

Canonical entry: `lean_weave:main` (`uv run lean-weave`). `src/main.py` is an IDE sample to remove/rehome later.

## MVP 1 exclusions

No MCP, ACP, Docker orchestration, background workers, multi-agent routers, or nested AI Factory agent launchers.

## ADRs

| ADR | Topic |
|-----|-------|
| [ADR-001](../.ai-factory/adr/ADR-001-component-boundaries.md) | Component boundaries |
| [ADR-002](../.ai-factory/adr/ADR-002-orchestrator-langgraph-vs-minimal.md) | Orchestrator choice |
| [ADR-003](../.ai-factory/adr/ADR-003-llm-provider-deepseek.md) | DeepSeek LLM provider |
| [ADR-004](../.ai-factory/adr/ADR-004-context-isolation-and-state.md) | Context isolation and state |

## See Also

- [Getting Started](getting-started.md) — install and verify
- [CLI Reference](cli.md) — commands, context preview, configuration
- [Standalone code tools](code-tools.md) — reads, patches, checks, Git metadata
- [Token budget principles](../.ai-factory/references/v1-01-token-budget-principles.md) — ContextProvider rules
- [MVP 1 acceptance criteria](../.ai-factory/references/v1-01-mvp1-acceptance-criteria.md) — architecture gate checklist
