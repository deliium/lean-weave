[Back to README](../README.md) · [Architecture →](architecture.md)

# Getting Started

## Prerequisites

- Python **≥ 3.12**
- [uv](https://docs.astral.sh/uv/) for sync and runs

## Install

From the repository root:

```bash
uv sync --all-groups
```

That installs runtime deps (`pydantic`), spike libs (`openai`, `langgraph`, …), and `pytest`.

Selective sync:

```bash
uv sync --group spikes --group dev
```

## Verify the install

```bash
uv run pytest tests/spikes tests/schemas tests/contracts
```

You should see **14 passed**. Live DeepSeek cases are skipped when `DEEPSEEK_API_KEY` is absent.

Stub CLI (MVP 1 hello only):

```bash
uv run lean-weave
# → Hello from lean-weave!
```

## Logging

Spike and future pipeline logging use:

```bash
LEAN_WEAVE_LOG_LEVEL=DEBUG uv run pytest tests/spikes -s
# fallback: LOG_LEVEL
```

## Spikes (not production CLI)

Bounded probes live under `spikes/`. Details: [spikes/README.md](../spikes/README.md).

| Probe | Purpose |
|-------|---------|
| `deepseek_probe` | Tool calling, thinking mode, JSON, usage fields |
| `langgraph_probe` | Checkpoint / interrupt / resume |
| `minimal_sm_probe` | plan → implement → verify → retry state machine |

Optional live DeepSeek:

```bash
export DEEPSEEK_API_KEY=...   # env only; never commit
PYTHONPATH=spikes uv run python -m deepseek_probe.probe
```

## Next steps

- Read [Architecture](architecture.md) for component boundaries and design choices
- Agent-facing detail: `.ai-factory/ARCHITECTURE.md` and ADRs under `.ai-factory/adr/`

## See Also

- [Architecture](architecture.md) — system design overview
- [spikes/README.md](../spikes/README.md) — probe setup and hygiene
- [DESCRIPTION](../.ai-factory/DESCRIPTION.md) — purpose and stack tags
