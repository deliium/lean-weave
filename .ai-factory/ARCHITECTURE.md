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
