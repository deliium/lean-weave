# Project Roadmap

> Token-efficient AI coding pipeline: structured FeatureState, DeepSeek, deterministic verify — minimize tokens without sacrificing quality.

## Milestones

- [x] **Architecture and technical validation (v1-01)** — Contracts, schemas, spikes, ADRs, isolation, MVP 1 acceptance gate
- [ ] **Git bootstrap** — Initialize repository, baseline ignore/hygiene, first commits of architecture artifacts
- [ ] **Core StateStore + MetricsCollector** — Persist FeatureState under `.lean-weave/`; token/usage counters only (no chat history)
- [ ] **LLMProvider (DeepSeek)** — Thinking/tools/`reasoning_content` echo, JSON mode caveats, usage fields; live probe when key available
- [ ] **ContextProvider** — Stage-scoped budgets, digests, allowlists; ban secrets/skill trees/cross-stage transcripts
- [ ] **Minimal WorkflowOrchestrator** — plan → implement → verify → retry/interrupt/resume on FeatureState (LangGraph remains deferred)
- [ ] **CodeExecutor + VerificationRunner** — Subprocess cwd jail; deterministic compile/test/schema gates (no LLM verify in MVP)
- [ ] **Thin CLI** — Feature start/resume/status commands wired to orchestrator; keep `lean_weave:main` canonical
- [ ] **End-to-end MVP feature loop** — One real feature path with metrics; no MCP/ACP/Docker/workers/multi-agent
- [ ] **Hardening** — Prompt-injection allowlists, executor path canonicalization, git-present isolation when `.git` exists
- [ ] **Optional LangGraph revisit** — Only if HITL/branching complexity justifies ADR-002 revisit

## Completed

| Milestone | Date |
|-----------|------|
| Architecture and technical validation (v1-01) | 2026-11-08 |
