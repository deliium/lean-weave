# v1-01 AI Factory Methodology Reuse (No Nested Agents)

Logged: `[MethodologyExtract.run]` via `LEAN_WEAVE_LOG_LEVEL` / `LOG_LEVEL`.

## Rule: no nested autonomous sessions

lean-weave **must not** launch nested IDE agent sessions, Task/Explore subagents, MCP Handoff sync loops, or nested `/aif-*` skill invocations to progress a feature. Workflow progression is owned by a single `WorkflowOrchestrator` driving stages with structured `FeatureState`. Skills are **methodology sources** (artifacts + procedures), not runtime executors inside the pipeline.

Rejected nested-agent patterns (do not port):

| Pattern | Source anchor | Why rejected |
|---------|---------------|--------------|
| Task / Explore subagents for codebase exploration | `aif-plan` allowed-tools + EXAMPLES “Explores codebase” | Multi-agent; token-heavy; MVP forbids multi-agent |
| AskUserQuestion preference/commit/docs loops | `aif-implement` Step 0.0 / 3.8 / 5 | Interactive IDE UX; pipeline uses CLI flags / persisted interrupts |
| MCP Handoff sync (`handoff_sync_status`, `handoff_push_plan`) | `aif-plan` / `aif-implement` Step 0 (pre) | MCP forbidden in MVP 1 |
| Nested `/aif-verify` after implement | `aif-implement` Final Step | Nested skill session; map to VerificationRunner instead |
| Nested `/aif-docs`, `/aif-commit`, `/aif-fix` | `aif-implement` completion prompts | Out-of-band human/CLI later; not in-graph agents |
| `--parallel` auto-invoke implement | `aif-plan` EXAMPLES | Autonomous nested implement session |

WARN: Context-gate severity (architecture/rules/roadmap) mixes LLM judgment with deterministic checks in `aif-verify` — ADR later decides which gates stay human/out-of-band vs automated (see VerificationRunner mapping).

## (a) Durable artifacts worth reusing

| Artifact / concept | Origin | lean-weave mapping |
|--------------------|--------|--------------------|
| Plan markdown with Settings (Testing/Logging/Docs) | `aif-plan` TASK-FORMAT | `FeaturePlan.settings` |
| Requirements Reconciliation table | `aif-plan` | `FeaturePlan.reconciliation` (structured, not prose dump) |
| Task checklist + dependencies + phases | TASK-FORMAT | `ImplementationTask[]` + dependency ids |
| Commit checkpoints | TASK-FORMAT / implement 3.8 | Optional `Checkpoint` markers in state; no auto-commit agent |
| Checkbox progress as source of truth | `aif-implement` 3.6 | Persist task status in `FeatureState` / StateStore |
| Ultra index vs phase detail split | ULTRA-FORMAT | Digests in context; full phase text only for active task |
| `aif-gate-result` JSON contract | `aif-verify` GATE-RESULT-CONTRACT | `VerificationResult` schema |
| Logging requirement checklist | TASK-FORMAT / LOGGING-GUIDE | Prompt fragment + VerificationRunner log-presence heuristics (optional) |

## (b) Agent-runtime behaviors that must NOT be reused

- Spawning TaskCreate/TaskList/TaskUpdate as control plane (use StateStore).
- Interactive AskUserQuestion for every preference (use plan settings + CLI).
- Handoff/MCP bidirectional sync.
- Invoking `/aif-verify`, `/aif-docs`, `/aif-commit` from inside implement.
- Loading entire skill markdown trees into context.
- Worktree merge / branch cleanup automation as nested skills (git isolation policy is separate; Task 10).

## (c) Prompt / procedure fragments worth reusing

Keep as **local templates** (short), not full skills:

1. **Plan stage template:** goal, constraints, settings, task list shape, reconciliation columns.
2. **Implement stage template:** one active task, files list, logging format `[Component.method]`, requirement evidence ids.
3. **Verify procedure:** ordered gates with pass/warn/fail; emit `aif-gate-result`-compatible JSON.
4. **Resume procedure:** load FeatureState → next pending task → stage-scoped context only.
5. **Requirement consistency check:** compare task behavior vs reconciliation rows (deterministic where citations are file paths / schemas).

## lean-weave stage mapping

```text
plan stage  → WorkflowOrchestrator.plan()
              ContextProvider: DESCRIPTION digest + ARCHITECTURE boundaries + request
              LLMProvider: produce FeaturePlan (+ tasks)
              StateStore: persist plan; no chat history

implement   → WorkflowOrchestrator.implement_next()
              ContextProvider: active ImplementationTask + file allowlist + prior task digests
              CodeExecutor: apply edits / run local commands
              StateStore: mark task complete / failed; interruptible

verify      → WorkflowOrchestrator.verify()
              VerificationRunner: deterministic gates (compile, tests, schema, file presence)
              Optional LLM only for non-deterministic review — deferred past MVP preference for deterministic
              MetricsCollector: token totals from plan+implement only

retry       → on verify fail: re-enter implement with failure digest only (no prior full transcripts)
```

Driven by **WorkflowOrchestrator**, never by spawning agents.

## VerificationRunner: deterministic vs human/out-of-band

| Gate (from aif-verify) | MVP 1 VerificationRunner | Notes |
|------------------------|--------------------------|-------|
| All required tasks complete / checkboxes match | Deterministic | StateStore vs plan |
| Plan integrity (ultra links, Task N mapping) | Deterministic | If ultra used |
| Build / `py_compile` / import check | Deterministic | CodeExecutor |
| Pytest (when Testing=yes) | Deterministic | CodeExecutor |
| File presence for task “Files:” lists | Deterministic | Path checks |
| `aif-gate-result` emission | Deterministic | Schema validate |
| Lint zero-warnings | Deterministic if tool configured; else skip/warn | Optional |
| Architecture / rules / roadmap context gates | Human / out-of-band for MVP 1 | Ambiguous LLM judgment |
| Requirements consistency (semantic) | Hybrid later; MVP: citation path existence + schema tests | WARN ambiguity |
| Docs checkpoint | Human / `/aif-docs` out-of-band | Per plan Docs setting |
| Suggest `/aif-fix` remediation | Out-of-band | Do not nest |

## Skills parsed (section counts approximate)

- `aif-plan`: workflow + formats (~12 major sections) + TASK-FORMAT / ULTRA-FORMAT
- `aif-implement`: steps 0–5 + critical rules (~15 major sections) + IMPLEMENTATION-GUIDE / LOGGING-GUIDE
- `aif-verify`: context load, gates, report, strict mode (~10 major sections) + gate contracts
