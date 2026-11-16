# ADR-001: Component Boundaries

- Status: Accepted
- Date: 2026-11-08

## Context

MVP 1 needs clear seams for a token-efficient coding pipeline without over-abstraction.

## Decision

Define eight components with Protocol contracts under `src/lean_weave/contracts/`:

CLI, LLMProvider, ContextProvider, WorkflowOrchestrator, CodeExecutor, VerificationRunner, StateStore, MetricsCollector.

Allowed dependency edges are documented in `v1-01-component-contracts.md`. Forbidden: MCP/ACP, Docker APIs, workers, multi-agent routers.

## Consequences

- Implementations can land in later plans behind stable Protocols.
- Tests import contracts without runtime behavior.
- Prevents “god CLI” and accidental LLM use in VerificationRunner for MVP.
