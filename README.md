# lean-weave

> Token-efficient AI coding pipeline — plan, implement, verify, and retry with structured state and minimal LLM tokens.

lean-weave automates feature development using DeepSeek, deterministic local verification, and a single-process workflow. **MVP 1** validates architecture (contracts, schemas, spikes, ADRs); the production feature runner comes in later plans.

## Quick Start

```bash
uv sync --all-groups
uv run pytest tests/spikes tests/schemas tests/contracts
```

Expected: **14 passed** (DeepSeek live cases skip unless `DEEPSEEK_API_KEY` is set).

## Key Features

- **Structured state** — `FeatureState` / plan / tasks / verification; not full chat history
- **Token budgets** — stage-scoped context with digests, allowlists, and hard bans on secrets
- **DeepSeek-ready** — OpenAI-compatible provider contract with thinking/tools/`reasoning_content` rules
- **Minimal orchestrator** — deterministic state machine for MVP 1; LangGraph deferred after spike validation
- **Deterministic verify** — prefer compile/tests/schema checks over LLM review

## Example

```bash
# Stub CLI (hello only in MVP 1)
uv run lean-weave

# Architecture gate suite
uv run pytest tests/spikes tests/schemas tests/contracts

# Optional live DeepSeek probe
export DEEPSEEK_API_KEY=...   # never commit
PYTHONPATH=spikes uv run python -m deepseek_probe.probe
```

---

## Documentation

| Guide | Description |
|-------|-------------|
| [Getting Started](docs/getting-started.md) | Install, sync, tests, logging, spikes |
| [Architecture](docs/architecture.md) | Components, orchestrator decision, boundaries |

### Agent artifacts

| Artifact | Description |
|----------|-------------|
| [DESCRIPTION](.ai-factory/DESCRIPTION.md) | Purpose and stack |
| [ARCHITECTURE](.ai-factory/ARCHITECTURE.md) | Boundaries, token strategy, ADRs |
| [Plan v1-01](.ai-factory/plans/v1-01-architecture.md) | Architecture validation plan |

## MVP 1 exclusions

No MCP, ACP, Docker orchestration, background workers, or multi-agent runtime.

## License

License not set yet.
