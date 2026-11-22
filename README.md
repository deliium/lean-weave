# lean-weave

> Token-efficient AI coding pipeline — plan, implement, verify, and retry with structured state and minimal LLM tokens.

lean-weave is a single-process feature workflow using DeepSeek and deterministic local verification. The public CLI exposes `wl feature` with isolated Git worktrees, `--auto` / `--review` modes, resume/cancel/status, and an offline MVP benchmark corpus under `benchmarks/`.

## Quick Start

```bash
uv sync
uv run wl init
uv run wl doctor
uv run wl context search "create_llm_provider" --json
uv run wl index build
uv run wl index status
uv run wl map show
# optional, potentially billable, requires an environment key:
# uv run wl llm check
```

The executable aliases are `wl` and `lean-weave`, both using the same app.
`lean_weave:main` remains the canonical Python entrypoint; `aif` is not an executable.

## Key Features

- **Structured state** — `FeatureState` / plan / tasks / verification; not full chat history
- **Token-budget design** — stage-scoped context with digests, allowlists, and hard bans on secrets
- **Deterministic context preview** — bounded discovery, AST symbols/imports, `rg`/Python search, provenance, progressive expansion, and prompt budgets without LLM calls
- **Disposable code index** — offline SQLite cache of Python facts with incremental updates, checked lookup, and per-checkout isolation; never a prompt authority and never an LLM call
- **Repository map** — compact ranked module/symbol views and static import edges from the checked index (`wl map`); optional `--indexed-map` context preview only
- **Budgeted DeepSeek gateway** — thinking/tools/JSON modes, bounded retries, per-feature budgets, measured-versus-estimated usage
- **Orchestrator design** — deterministic state machine for MVP 1; LangGraph deferred after spike validation
- **Durable workflow** — atomic `.lean-weave/workflow/` checkpoints, bounded retries, explicit results, and no chat-history state
- **Deterministic verification design** — prefer compile/tests/schema checks over LLM review

## Example

```bash
# Inspect configuration without printing secrets
uv run wl config show --format json

# Offline context: signatures first, then complete ranges when requested
uv run wl context build "wire the provider seam" \
  --path src/lean_weave/app.py --level ranges --json
uv run wl feature --help
```

---

## Documentation

| Guide | Description |
|-------|-------------|
| [Getting Started](docs/getting-started.md) | Install, sync, tests, logging, spikes |
| [Architecture](docs/architecture.md) | Components, orchestrator decision, boundaries |
| [CLI Reference](docs/cli.md) | Commands, configuration, errors, injection |
| [Code tools](docs/code-tools.md) | Offline reads, patches, checks, and Git metadata |
| [Code index](docs/code-index.md) | Disposable SQLite cache, maintenance, recovery, isolation |
| [Context cache](docs/context-cache.md) | Opt-in reuse of deterministic analysis, retrieval, and packing work |
| [Context retrieval](docs/context-retrieval.md) | Task-aware hybrid ranking, CLI preview, evaluation |
| [Context packing](docs/context-packing.md) | Token budgets, L0/L1/L2, sessions, guarded DeepSeek delivery |
| [Repository map](docs/repository-map.md) | Compact offline maps, budgets, and indexed context preview |
| [Worktree workflow](docs/worktree-workflow.md) | Feature CLI, approvals, cancel, cleanup |
| [Benchmarks](docs/benchmarks.md) | Fixture corpus, offline evidence, live preflight, V2 protocol |
| [MVP 1 readiness](docs/mvp1-readiness.md) | Offline/live/safety verdicts and evidence links |

### Agent artifacts

| Artifact | Description |
|----------|-------------|
| [DESCRIPTION](.ai-factory/DESCRIPTION.md) | Purpose and stack |
| [ARCHITECTURE](.ai-factory/ARCHITECTURE.md) | Boundaries, token strategy, ADRs |
| [Plan v1-01](.ai-factory/plans/v1-01-architecture.md) | Architecture validation plan |
| [Plan v1-03](.ai-factory/plans/v1-03-deepseek-gateway.md) | DeepSeek gateway and token metrics |
| [Plan v1-04](.ai-factory/plans/v1-04-context-builder.md) | Deterministic context builder |
| [Plan v1-05](.ai-factory/plans/v1-05-code-tools.md) | Safe code editing and verification tools |
| [Plan v1-06](.ai-factory/plans/v1-06-orchestrator.md) | Minimal deterministic workflow orchestrator |
| [Plan v1-08](.ai-factory/plans/v1-08-e2e-validation.md) | End-to-end validation and token baseline |

## MVP 1 exclusions

No MCP, ACP, Docker orchestration, background workers, or multi-agent runtime.

## License

[CC0 1.0 Universal](LICENSE).
