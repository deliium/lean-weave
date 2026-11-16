# v1-01 Component Contracts

Logged: `[Contracts.define]` via `LEAN_WEAVE_LOG_LEVEL` / `LOG_LEVEL`.  
Code: `src/lean_weave/contracts/` (Protocol typing only).

## MVP exclusions (all components)

Forbidden: MCP/ACP clients, Docker orchestration, background workers, multi-agent routers, nested `/aif-*` agent sessions.

## Dependency graph (allowed)

```text
CLI → WorkflowOrchestrator
WorkflowOrchestrator → ContextProvider, LLMProvider, CodeExecutor, VerificationRunner, StateStore, MetricsCollector
ContextProvider → (filesystem read, budgets)  // no LLM
VerificationRunner → CodeExecutor            // deterministic gates
LLMProvider → DeepSeek HTTP only
StateStore → .lean-weave/ filesystem (+ optional git metadata read)
```

Rejected edges: `LLMProvider → StateStore` (no secret prompts persisted by provider); `ContextProvider → LLMProvider`; `VerificationRunner → LLMProvider` (MVP); `CLI → LLMProvider` direct.

## Per-component summary

| Component | Responsibility | Sync/Async | Error model | Logging |
|-----------|----------------|------------|-------------|---------|
| CLI | argv → orchestrator; exit codes | sync | argparse errors → 2; feature fail → 1 | `[CLI.run]` |
| LLMProvider | DeepSeek chat; thinking/tools/`reasoning_content`/usage | sync MVP | typed provider errors; never leak key | `[LLMProvider.complete]` metadata only |
| ContextProvider | stage-scoped bundles under budgets | sync | BudgetExceeded / ForbiddenContext | `[ContextProvider.build]` |
| WorkflowOrchestrator | minimal SM transitions; interrupt/resume | sync | invalid transition; propagate child errors | `[Orchestrator.*]` |
| CodeExecutor | local subprocess; cwd jail; network off default | sync | non-zero / timeout results (not always raise) | `[CodeExecutor.run]` redacted |
| VerificationRunner | deterministic gates → VerificationResult | sync | gate fail → result status fail | `[VerificationRunner.run]` |
| StateStore | load/save FeatureState JSON | sync | missing feature; IO errors | `[StateStore.save]` |
| MetricsCollector | token counters by feature/stage | sync | ignore unknown fields | `[MetricsCollector.record_usage]` |

## LLMProvider (DeepSeek findings)

- Inputs: messages, optional tools, `thinking` enabled/disabled, optional `response_format`, stage tag.
- Outputs: assistant message (`content`, optional `reasoning_content`, `tool_calls`) + usage (`prompt`, `completion`, `reasoning_tokens`, cache hits).
- When tools present, caller must echo prior `reasoning_content` on subsequent turns (orchestrator/tool-loop helper — not FeatureState history).
