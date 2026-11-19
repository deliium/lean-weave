# lean-weave

> Token-efficient AI coding pipeline — plan, implement, verify, and retry with structured state and minimal LLM tokens.

lean-weave is building a single-process feature workflow using DeepSeek and deterministic local verification. **v1-04** adds an offline, token-efficient context builder: bounded repository discovery, AST symbols/imports, literal search with an `rg`/Python fallback, deterministic ranking, provenance, and prompt budgets. The production feature runner comes in later plans.

## Quick Start

```bash
uv sync
uv run wl init
uv run wl doctor
uv run wl context search "create_llm_provider" --json
# optional, potentially billable, requires an environment key:
# uv run wl llm check
```

The executable aliases are `wl` and `lean-weave`, both using the same app.
`lean_weave:main` remains the canonical Python entrypoint; `aif` is not an executable.

## Key Features

- **Structured state** — `FeatureState` / plan / tasks / verification; not full chat history
- **Token-budget design** — stage-scoped context with digests, allowlists, and hard bans on secrets
- **Deterministic context preview** — bounded discovery, AST symbols/imports, `rg`/Python search, provenance, progressive expansion, and prompt budgets without LLM calls
- **Budgeted DeepSeek gateway** — thinking/tools/JSON modes, bounded retries, per-feature budgets, measured-versus-estimated usage
- **Orchestrator design** — deterministic state machine for MVP 1; LangGraph deferred after spike validation
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

### Agent artifacts

| Artifact | Description |
|----------|-------------|
| [DESCRIPTION](.ai-factory/DESCRIPTION.md) | Purpose and stack |
| [ARCHITECTURE](.ai-factory/ARCHITECTURE.md) | Boundaries, token strategy, ADRs |
| [Plan v1-01](.ai-factory/plans/v1-01-architecture.md) | Architecture validation plan |
| [Plan v1-03](.ai-factory/plans/v1-03-deepseek-gateway.md) | DeepSeek gateway and token metrics |
| [Plan v1-04](.ai-factory/plans/v1-04-context-builder.md) | Deterministic context builder |
| [Plan v1-05](.ai-factory/plans/v1-05-code-tools.md) | Safe code editing and verification tools |

## MVP 1 exclusions

No MCP, ACP, Docker orchestration, background workers, or multi-agent runtime.

## License

[CC0 1.0 Universal](LICENSE).
