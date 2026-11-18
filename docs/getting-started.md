[Back to README](../README.md) · [Architecture →](architecture.md)

# Getting Started

## Prerequisites

- Python **≥ 3.12**
- [uv](https://docs.astral.sh/uv/) for sync and runs

## Install

From the repository root:

```bash
uv sync
```

That installs runtime dependencies (Pydantic, Typer, Click, PyYAML) and pytest.
Dependency acquisition may require a package index or a populated cache.

## First run

```bash
uv run wl init
uv run wl config show
uv run wl doctor
uv run wl feature --help
```

`init` prints the new `.lean-weave/config.yaml` path; running it again refuses
to overwrite without `--force`. Doctor reports local prerequisites; an absent
API key is informational and does not prevent offline use.

The aliases `wl` and `lean-weave` invoke the same application. `aif` is not
an executable. `lean_weave:main` is the canonical Python entrypoint.

## Verify offline

Prepare one of these environments before running offline tests:

```bash
# Core (no spike libraries)
uv sync
uv run --offline --no-sync pytest --ignore=tests/spikes

# Full architecture regression (explicit optional dependencies)
uv sync --group spikes
uv run --offline --no-sync pytest
```

Plain `uv sync` does not support full-suite collection: architecture spikes
import optional packages. Test execution requires no API access. An autouse
socket guard blocks IPv4/IPv6 connections unless a test explicitly carries
`@pytest.mark.network`; no foundation test uses that marker. Unix sockets
and subprocesses remain allowed. The guard only covers the pytest process,
not child processes or collection-time imports.

## Logging

Application logs go to stderr; configuration output goes to stdout.

```bash
LEAN_WEAVE_LOG_LEVEL=DEBUG uv run wl config show
uv run wl --log-format json doctor
# fallback: LOG_LEVEL
```

Level precedence: flag > `LEAN_WEAVE_LOG_LEVEL` > `LOG_LEVEL` > YAML > INFO.
Format precedence: flag > YAML > text. Environment overrides are not persisted.
See [CLI Reference](cli.md) for redaction and configuration rules.

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
- [CLI Reference](cli.md) — commands and configuration
- [spikes/README.md](../spikes/README.md) — probe setup and hygiene
