[← Architecture](architecture.md) · [Back to README](../README.md)

# CLI Reference

`wl` and `lean-weave` are executable aliases for one Typer application.
The canonical Python entrypoint is `lean_weave:main`. There is no `aif` executable.

## Commands

Global options precede the command:

```bash
uv run wl --config settings.yaml --log-level DEBUG --log-format json config show
```

| Command | Behavior |
|---------|----------|
| `wl init [--force]` | Create commented defaults; refuse overwrite unless forced |
| `wl config show [--format yaml\|json]` | Print file/default settings and `api_key_present` |
| `wl doctor` | Check Python, package, config, state-directory writability, git and key presence |
| `wl feature` / `wl feature --help` | Display help only; no workflow execution |
| `wl --help` | Show commands and global options |

Initialization and all help paths bypass config loading. This supports repair:

```bash
uv run wl --config new/settings.yaml init
uv run wl --config new/settings.yaml init --force
```

Doctor requires Python >=3.12. Config errors exit 3; other critical failures
exit 1. Missing git or the configured API key only produces a warning.
The configured state path is checked, not a hard-coded directory. If missing,
doctor probes its nearest existing parent and warns; it never creates directories.
Existing file targets fail. Writability probes are temporary files removed
after checking, including when writing fails. No network request is made.

## Configuration

Product defaults live at `.lean-weave/config.yaml`, **not**
`.ai-factory/config.yaml` (AI Factory tooling).
The MVP project root is the current working directory; no ancestor discovery
is performed. Run commands from the intended project root.

An existing implicit default is loaded. Only a missing implicit default falls
back to defaults with a warning. An explicit missing `--config` is an error,
as are malformed YAML, unknown keys, invalid types and unsupported versions.
Empty files are not valid mapping documents. YAML uses safe loading.

```yaml
version: 1
project:
  name: lean-weave
llm:
  provider: deepseek
  model: deepseek-flash
  base_url: https://api.deepseek.com
  thinking: false
  api_key_env: DEEPSEEK_API_KEY
  timeout_s: 60.0
logging:
  level: INFO
  format: text
workflow:
  state_dir: .lean-weave
  max_retries: 2
```

Partial mappings merge with model defaults. All sections reject unknown keys.
`provider` supports only `deepseek`; `timeout_s` must be finite and positive;
`max_retries` must be a non-negative integer. Logging levels are DEBUG, INFO,
WARNING, ERROR, CRITICAL; formats are text or json.
`version` supports only 1. `api_key_env` must match `^[A-Z_][A-Z0-9_]*$`.
`state_dir` must be non-empty and relative, with no `..` segments; resolved
symlinks must stay inside the project root.

Secrets are environment-only. Set the variable named by `llm.api_key_env`
through your environment/secret manager; never put a key in YAML.
`config show` reports only whether that variable has a non-empty value.
It does not apply logging environment overrides to the printed configuration.

## Logging and redaction

- Logs: stderr. Config YAML/JSON and command results: stdout.
- Level: `--log-level` > `LEAN_WEAVE_LOG_LEVEL` > `LOG_LEVEL` > YAML > INFO.
- Format: `--log-format` > YAML > text.
- Invalid flag/environment levels warn and fall back to INFO without echoing input.
- JSON records contain timestamp, level, logger, message, and optional extra fields.
- Redaction masks sensitive mapping keys (`api_key`, `token`, `secret`,
  `authorization`, `password`), including nested mappings/sequences.
- The current default and configured secret-env values are masked in messages,
  interpolated arguments and exceptions. Configuration validation reports only
  safe fields/error codes or YAML positions, not input payloads.
- Schema/Protocol imports do not configure logging or emit records.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success or help |
| 1 | Operational failure, init refusal, unavailable dependency, doctor critical failure |
| 2 | Usage/parser error |
| 3 | Configuration read/parse/validation failure |

Expected failures print safe errors without raw tracebacks.

## Dependency injection

`AppContext` exposes typed optional Protocol slots. No provider or orchestration
implementation is constructed by default; no registry or service locator is used.

```python
from lean_weave.app import build_app_context
from lean_weave.schemas import FeatureState


class FakeStore:
    def load(self, feature_id: str) -> FeatureState:
        return FeatureState(feature_id=feature_id)

    def save(self, feature_state: FeatureState) -> None:
        self.saved = feature_state


fake = FakeStore()
context = build_app_context(state_store=fake)
assert context.state_store is fake
```

The builder supports `llm_provider`, `context_provider`, `code_executor`,
`verification_runner`, `state_store`, `metrics_collector`, and `orchestrator`.
Consumers inspect typed attributes and can raise `DependencyUnavailable` when
a required component is absent. `FeatureRequest`, `FeaturePlan`,
`ImplementationTask`, `FeatureState`, `VerificationResult` and `TokenUsage`
support Pydantic JSON serialization.

## Not implemented in v1-02

No LLM calls, code editing, orchestration execution, LangGraph runtime,
MCP/ACP clients, Docker orchestration, workers or background services.
Architecture spikes remain optional validation tools, not the product runner.

## See Also

- [Getting Started](getting-started.md) — installation and offline test environments
- [Architecture](architecture.md) — component boundaries and decisions
