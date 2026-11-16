# lean-weave Architecture

## Overview

lean-weave is a **single-process CLI pipeline** that plans, implements, verifies, and retries feature work using:

- structured `FeatureState` (not chat history),
- DeepSeek via `LLMProvider`,
- stage-scoped `ContextProvider` budgets,
- local `CodeExecutor` + deterministic `VerificationRunner`,
- `StateStore` under `.lean-weave/`,
- `MetricsCollector` for token counters.

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
