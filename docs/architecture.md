[← Getting Started](getting-started.md) · [Back to README](../README.md)

# Architecture

lean-weave is a **single-process** coding pipeline: plan → implement → verify → retry while minimizing LLM tokens.

Authoritative agent docs: [ARCHITECTURE.md](../.ai-factory/ARCHITECTURE.md). This page is the human overview.

## Design choices

- **Structured state**, not chat logs — `FeatureState`, `FeaturePlan`, tasks, `VerificationResult`
- **DeepSeek** as the MVP LLM (OpenAI-compatible API) with explicit thinking / tools / `reasoning_content` handling
- **Minimal state machine** orchestrator for MVP 1; LangGraph interrupt/checkpoint capabilities were validated in spikes and **deferred**
- **Deterministic verification** (compile / tests / schema / file checks) preferred over LLM review
- **Stage-scoped context** with hard bans on secrets, full skill trees, and cross-stage transcripts

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

## Orchestrator decision

| Option | MVP 1 |
|--------|-------|
| Minimal Python state machine | **Adopted** |
| LangGraph | Deferred (spikes retained) |

Evidence: [orchestrator comparison](../.ai-factory/references/v1-01-orchestrator-comparison.md) · [ADR-002](../.ai-factory/adr/ADR-002-orchestrator-langgraph-vs-minimal.md).

Methodology from `aif-plan` / `aif-implement` / `aif-verify` is reused as **artifacts + procedures**, not nested agent sessions.

## Package layout (target)

```text
src/lean_weave/   # cli, providers, context, orchestrator, … (schemas/contracts exist)
spikes/           # bounded probes — not production CLI
tests/            # spikes / schemas / contracts
.lean-weave/      # runtime FeatureState (gitignored)
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
- [Token budget principles](../.ai-factory/references/v1-01-token-budget-principles.md) — ContextProvider rules
- [MVP 1 acceptance criteria](../.ai-factory/references/v1-01-mvp1-acceptance-criteria.md) — architecture gate checklist
