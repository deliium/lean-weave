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

That installs runtime dependencies (Pydantic, Typer, Click, PyYAML, pathspec,
and the OpenAI-compatible SDK used by the DeepSeek gateway) and pytest.
Dependency acquisition may require a package index or a populated cache.

## First run

```bash
uv run wl init
uv run wl config show
uv run wl doctor
uv run wl feature --help
uv run wl context --help
uv run wl index --help
```

`init` prints the new `.lean-weave/config.yaml` path; running it again refuses
to overwrite without `--force`. Doctor reports local prerequisites; an absent
API key is informational and does not prevent offline use.

## Offline context preview

Context retrieval is deterministic and needs no credentials:

```bash
uv run wl context search "create_llm_provider" --json
uv run wl context build "wire the provider seam" \
  --path src/lean_weave/app.py --level ranges --json
uv run wl context inspect src/lean_weave/app.py --symbol create_llm_provider
```

`build` defaults to a compact map plus selected signatures. `--level ranges`
adds complete AST spans; `--level full` requires exactly one path and a
`--reason`. Every excerpt includes its path, original line range, and snapshot
digest. Budgets and exclusions are configured under `context` in
`.lean-weave/config.yaml`.

## Persistent code index (optional, offline)

The index is a disposable local cache of Python facts. It is never required:
without it, context builds use the normal AST parser.

```bash
uv run wl index build
uv run wl index status
uv run wl map show
# after editing or switching branches:
uv run wl index update
# recovery when status reports corrupt/incompatible/recovery_required:
uv run wl index clear --yes
uv run wl index build
```

The cache lives under `${XDG_CACHE_HOME:-~/.cache}/lean-weave/code-index/` and
is isolated per checkout. It makes no LLM calls and is never written into the
repository. See [Code index](code-index.md) for freshness, stable identifiers,
limits, platform behavior, and recovery. After a current index exists, see
[Repository map](repository-map.md) for compact offline map queries.

## Feature workflow (worktree + CLI)

Public feature commands require Git and an operator config **outside** the
repository. Repository defaults alone never authorize writes or checks. API
keys stay in the environment (`DEEPSEEK_API_KEY`). See
[Worktree workflow](worktree-workflow.md).

```bash
uv run wl --config /secure/operator.yaml feature "describe the change" --review
uv run wl --config /secure/operator.yaml resume <feature-id> --approve <token>
uv run wl status <feature-id>
uv run wl result <feature-id>
uv run wl cancel <feature-id>
```

Typed feature outcomes (success, failure, budget exhaustion, interruption,
cancellation) render as `key=value` lines and exit `0`. Parser misuse exits
`2`; configuration errors exit `3`; other operational failures exit `1`.

## Offline MVP validation

```bash
uv run --locked pytest tests/cli/test_feature_offline_e2e.py tests/benchmarks/
```

See [Benchmarks](benchmarks.md) for fixtures, metric units, and the explicit
live DeepSeek preflight.

## Library workflow entry point

The minimal orchestrator remains a library seam. Writes and checks still
require an explicit caller approval snapshot, so YAML alone never authorizes
them.

```python
from pathlib import Path

from lean_weave.app import build_app_context, create_orchestrator
from lean_weave.executor.approval import approve_tools
from lean_weave.workflow.planning import SingleTaskSpec

context = build_app_context()
approval = approve_tools(
    Path.cwd(),
    context.config,
    allow_writes=True,
    allow_checks=True,
)
orchestrator = create_orchestrator(
    context,
    approval=approval,
    check_ids=["parse"],  # must exist in tools.checks
)
state = orchestrator.run_simple(
    "feat-example",
    "add one marker comment",
    SingleTaskSpec(
        id="1",
        title="edit one file",
        file="src/lean_weave/app.py",
        check_ids=["parse"],
    ),
)
result = orchestrator.result("feat-example")
print(result.status.value, result.reason)
```

Result statuses:

| Status | Meaning |
|--------|---------|
| `success` | Every required gate passed; no later LLM call is made |
| `failed` | Explicit verification/plan/approval failure, including retry exhaustion |
| `budget_exhausted` | A call or token limit denied the next dispatch |
| `interrupted` | Provider error, unresolved effect, or approval revalidation needs a resume |

Checks that execute workspace code need an injected offline isolation backend;
without one the workflow refuses before editing. Patches are limited to the
active task's approved files and current revision. A committed patch whose
durability cannot be confirmed interrupts with its effect marker intact and is
never replayed. Durable checkpoints live under `.lean-weave/workflow/`; use
`resume(feature_id)` after a restart and `result(feature_id)` for a read-only
summary.

## Optional live DeepSeek check

`wl llm check` is the only ordinary command that contacts DeepSeek. It sends a
single small fixed request and is potentially billable. Set the environment key
first; never place it in YAML:

```bash
export DEEPSEEK_API_KEY=...
uv run wl llm check
```

The command prints only safe status, model, and reported usage metadata. It
forces one attempt regardless of the configured retry ceiling. Live verification
remains unverified when no real credentials or spend approval are available;
offline verification is separate.

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
import optional packages. Test execution requires no API access. Gateway tests
drive the real locked SDK through an in-memory HTTP transport; they never use a
network marker. An autouse socket guard blocks IPv4/IPv6 connections unless a
test explicitly carries `@pytest.mark.network`; no foundation or gateway test
uses that marker. Unix sockets and subprocesses remain allowed. The guard only
covers the pytest process, not child processes or collection-time imports.

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
