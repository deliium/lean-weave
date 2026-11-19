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

## Component boundaries

```text
CLI → WorkflowOrchestrator
        ├─ ContextProvider   (deterministic allowlists / budgets)
        ├─ LLMProvider       (DeepSeek only in MVP)
        ├─ CodeExecutor      (subprocess, cwd jail)
        ├─ VerificationRunner (no LLM in MVP)
        ├─ StateStore        (.lean-weave/)
        └─ MetricsCollector  (token counters)
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

- Inventory is path-only; bodies are read only for admitted candidates.
- Ranking stays inside an eligible stage scope; it never widens it.
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

## Orchestrator decision

| Option | MVP 1 |
|--------|-------|
| Minimal Python state machine | **Adopted** |
| LangGraph | Deferred (spikes retained) |

Evidence: [orchestrator comparison](../.ai-factory/references/v1-01-orchestrator-comparison.md) · [ADR-002](../.ai-factory/adr/ADR-002-orchestrator-langgraph-vs-minimal.md).

Methodology from `aif-plan` / `aif-implement` / `aif-verify` is reused as **artifacts + procedures**, not nested agent sessions.

## Package layout (target)

```text
src/lean_weave/   # cli, config, context, providers, metrics, contracts, schemas, app
spikes/           # bounded probes — not production CLI
tests/            # config / contracts / providers / metrics / cli / context
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
- [Token budget principles](../.ai-factory/references/v1-01-token-budget-principles.md) — ContextProvider rules
- [MVP 1 acceptance criteria](../.ai-factory/references/v1-01-mvp1-acceptance-criteria.md) — architecture gate checklist
