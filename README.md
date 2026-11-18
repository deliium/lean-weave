# lean-weave

> Token-efficient AI coding pipeline — plan, implement, verify, and retry with structured state and minimal LLM tokens.

lean-weave is building a single-process feature workflow using DeepSeek and deterministic local verification. **v1-02** provides the offline CLI, validated configuration, serializable models, and typed injection foundation; the production feature runner comes in later plans.

## Quick Start

```bash
uv sync
uv run wl init
uv run wl doctor
```

The executable aliases are `wl` and `lean-weave`, both using the same app.
`lean_weave:main` remains the canonical Python entrypoint; `aif` is not an executable.

## Key Features

- **Structured state** — `FeatureState` / plan / tasks / verification; not full chat history
- **Token-budget design** — stage-scoped context with digests, allowlists, and hard bans on secrets
- **DeepSeek-ready** — OpenAI-compatible provider contract with thinking/tools/`reasoning_content` rules
- **Orchestrator design** — deterministic state machine for MVP 1; LangGraph deferred after spike validation
- **Deterministic verification design** — prefer compile/tests/schema checks over LLM review

## Example

```bash
# Inspect configuration without printing secrets
uv run wl config show --format json
uv run wl feature --help
```

---

## Documentation

| Guide | Description |
|-------|-------------|
| [Getting Started](docs/getting-started.md) | Install, sync, tests, logging, spikes |
| [Architecture](docs/architecture.md) | Components, orchestrator decision, boundaries |
| [CLI Reference](docs/cli.md) | Commands, configuration, errors, injection |

### Agent artifacts

| Artifact | Description |
|----------|-------------|
| [DESCRIPTION](.ai-factory/DESCRIPTION.md) | Purpose and stack |
| [ARCHITECTURE](.ai-factory/ARCHITECTURE.md) | Boundaries, token strategy, ADRs |
| [Plan v1-01](.ai-factory/plans/v1-01-architecture.md) | Architecture validation plan |

## MVP 1 exclusions

No MCP, ACP, Docker orchestration, background workers, or multi-agent runtime.

## License

[CC0 1.0 Universal](LICENSE).
