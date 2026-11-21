# Project Roadmap

> Token-efficient AI coding pipeline: structured FeatureState, DeepSeek, deterministic verify — minimize tokens without sacrificing quality.

## Milestones

- [x] **Architecture and technical validation (v1-01)** — Contracts, schemas, spikes, ADRs, isolation, MVP 1 acceptance gate
- [x] **Git bootstrap** — Initialize repository, baseline ignore/hygiene, first commits of architecture artifacts
- [x] **Core StateStore + MetricsCollector** — Persist FeatureState under `.lean-weave/`; token/usage counters only (no chat history)
- [x] **LLMProvider (DeepSeek)** — Thinking/tools/`reasoning_content` echo, JSON mode caveats, usage fields; live probe when key available
- [x] **ContextProvider** — Stage-scoped budgets, digests, allowlists; ban secrets/skill trees/cross-stage transcripts
- [x] **Minimal WorkflowOrchestrator** — plan → implement → verify → retry/interrupt/resume on FeatureState (LangGraph remains deferred)
- [x] **CodeExecutor + VerificationRunner** — Subprocess cwd jail; deterministic compile/test/schema gates (no LLM verify in MVP)
- [x] **Thin CLI** — Feature start/resume/status commands wired to orchestrator; keep `lean_weave:main` canonical
- [x] **End-to-end MVP feature loop** — Offline CLI/worktree acceptance and token baseline corpus (live DeepSeek capture optional; see `docs/mvp1-readiness.md`)
- [ ] **Hardening** — Prompt-injection allowlists, executor path canonicalization, git-present isolation when `.git` exists
- [ ] **Optional LangGraph revisit** — Only if HITL/branching complexity justifies ADR-002 revisit

## Completed

| Milestone | Date |
|-----------|------|
| Architecture and technical validation (v1-01) | 2026-11-08 |
| Git bootstrap | 2026-11-18 |
| LLMProvider (DeepSeek) | 2026-11-18 |
| ContextProvider (deterministic offline preview) | 2026-11-19 |
| CodeExecutor + VerificationRunner | 2026-11-19 |
| Core StateStore + MetricsCollector | 2026-11-20 |
| Minimal WorkflowOrchestrator | 2026-11-20 |
| Thin CLI | 2026-11-20 |
| End-to-end MVP feature loop | 2026-11-21 |
