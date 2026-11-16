# Spikes

Bounded technical probes for architecture validation. **Not** production CLI.

## Setup

```bash
# Runtime + spike + dev groups
uv sync --all-groups

# Or selectively:
uv sync --group spikes --group dev
```

Packages are intentionally **unpinned beyond lower bounds** so spikes track current DeepSeek/LangGraph APIs during validation. Production pins come in later plans.

## Run tests

```bash
uv run pytest tests/spikes tests/schemas tests/contracts
```

## Probes

| Directory | Purpose |
|-----------|---------|
| `deepseek_probe/` | DeepSeek tool calling, thinking mode, JSON, usage fields |
| `langgraph_probe/` | StateGraph checkpoint / interrupt / resume |
| `minimal_sm_probe/` | Minimal plan→implement→verify→retry state machine |

## Logging

```bash
LEAN_WEAVE_LOG_LEVEL=DEBUG uv run pytest tests/spikes -s
# fallback: LOG_LEVEL
```

## Hygiene

- Checkpoint SQLite files under `spikes/` are gitignored.
- Never commit `.env` or API keys. Live DeepSeek probes use `DEEPSEEK_API_KEY` from the environment only.
