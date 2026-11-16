# ADR-004: Context Isolation and State

- Status: Accepted
- Date: 2026-11-08

## Context

Token efficiency and security require stage isolation and chat-history-independent state.

## Decision

- `FeatureState` (schema v1.0.0) is the control plane: plan, tasks, verification, metrics, interrupt — **no message log**.
- ContextProvider enforces stage budgets and allowlists (token-budget principles).
- Git absent → StateStore only under `.lean-weave/`. Git present → branch/worktree isolation; no destructive git; no push by default.
- Secrets env-only; never persisted in state or prompts.

## Consequences

- Resume/retry works from structured state + digests.
- Prompt-injection residual risk mitigated by allowlists; full OS sandbox deferred.
