# Implementation Plan: Python CLI and Project Foundation

Plan ID: v1-02-project-foundation
Branch: none (exact plan identifier requested; no branch or worktree created)
Created: 2026-11-16

## Original Request

Plan ID: v1-02-project-foundation
Plan Title: Python CLI and Project Foundation

Dependencies:
- v1-01-architecture

Goal:
Implement a minimal, testable Python foundation for the AI coding pipeline.

Before planning:
- Read architecture documents and ADRs from v1-01.
- Inspect existing code.
- Preserve approved interfaces.
- Do not duplicate completed functionality.

Requirements:
1. Create a Python package using src-layout.
2. Configure pyproject.toml and dependency management.
3. Implement Typer CLI with the executable name `aif`.
4. Implement YAML configuration loading and validation.
5. Define typed interfaces for:
   - LLMProvider
   - ContextProvider
   - CodeExecutor
   - VerificationRunner
   - StateStore
   - MetricsCollector
6. Define shared domain models:
   - FeatureRequest
   - FeaturePlan
   - ImplementationTask
   - FeatureState
   - VerificationResult
   - TokenUsage
7. Implement initial CLI commands:
   - aif init
   - aif config show
   - aif doctor
   - aif feature --help
8. Implement consistent error handling and exit codes.
9. Configure structured application logging.
10. Add unit tests and basic developer documentation.

Constraints:
- No actual LLM calls.
- No code editing agent.
- No LangGraph execution yet.
- No MCP, Docker, or background services.
- Use environment variables for secrets.
- All tests must work without external API access.
- Prefer simple, explicit interfaces over speculative abstractions.

Acceptance criteria:
- The package installs successfully.
- CLI commands work.
- Configuration validation works.
- Domain models support serialization.
- Tests pass offline.
- The foundation supports dependency injection.

Save the plan using the exact identifier:
v1-02-project-foundation

## Settings
- Testing: yes
- Logging: verbose
- Docs: yes

## Roadmap Linkage
Milestone: "Thin CLI"
Rationale: This plan builds the CLI, configuration, logging, and injection foundation that the Thin CLI milestone depends on.

## Requirements Reconciliation

Authority: User request and follow-up CLI decision > `RULES.md` and ADR-001/003/004 > v1-01 reference artifacts (`v1-01-component-contracts.md`, `v1-01-state-schemas.md`, `v1-01-project-structure.md`, `v1-01-isolation-security.md`) > existing repository code. The user's follow-up answer "use wl instead of aif alias" overrides requirement 3's `aif` executable name.

| Decision / supported combination | Source path and section | Verification evidence |
|----------------------------------|-------------------------|-----------------------|
| Reuse the existing `lean_weave` package and `src/` layout; do not create a second package or duplicate existing contracts/schemas | User request — Requirement 1; `v1-01-project-structure.md` — Package layout; `src/lean_weave/` | Package remains `lean_weave`; all new modules live under `src/lean_weave/` |
| Console script is `wl`, not `aif`; `lean_weave:main` stays the canonical Python entrypoint; `wl` and `lean-weave` are aliases to the same Typer app | User follow-up — "use wl instead of aif alias"; `RULES.md` — canonical entrypoint rule | `pyproject.toml` has `wl` and `lean-weave` scripts; both invoke one `lean_weave.cli` app; `src/main.py` untouched |
| Existing six Protocols are extended and type-tightened, never re-created under new names | `v1-01-component-contracts.md`; `src/lean_weave/contracts/`; ADR-001 | `tests/contracts/` still pass; fake implementations structurally satisfy Protocols |
| Existing `FeaturePlan`, `ImplementationTask`, `FeatureState`, `VerificationResult` are preserved; `FeatureRequest` and Pydantic `TokenUsage` are added | `v1-01-state-schemas.md`; `src/lean_weave/schemas/models.py`; ADR-004 | Existing schema tests pass unchanged; new JSON round-trip tests pass |
| `contracts._types.TokenUsage` TypedDict is replaced by a re-export of the Pydantic `TokenUsage` model; field names stay compatible | `v1-01-state-schemas.md`; `contracts/_types.py` | Metrics contract annotation resolves to one model; no duplicate TokenUsage definitions |
| Product config lives at `.lean-weave/config.yaml`; `.ai-factory/config.yaml` remains AI Factory tooling config and is not reused at runtime | User answer — config location; ADR-004 — state under `.lean-weave/` | Config loader defaults to `.lean-weave/config.yaml`; tests prove `.ai-factory/config.yaml` is not read |
| Secrets are env-only (`DEEPSEEK_API_KEY` by default, or the configured `llm.api_key_env`), never loaded into models, dumped by `config show`, or logged | `RULES.md` — secret rules; `v1-01-isolation-security.md` | `config show` prints only `api_key_present: true/false`; tests cover default and custom env names and assert no key value in output/logs |
| Tests are fully offline: no HTTP and no live DeepSeek calls; key presence may be checked only via `os.environ` | User request — Constraints; `RULES.md` | `tests/conftest.py` socket guard; `tests/test_dependency_policy.py`; suite passes with `DEEPSEEK_API_KEY` unset and with a dummy value |
| Explicit `--config` missing is an error; implicit default missing uses defaults; `state_dir` cannot escape the project root | User request — Requirement 4; ADR-004 — state under `.lean-weave/`; `RULES.md` — state isolation | Loader tests for missing explicit/implicit paths, absolute/`..` `state_dir`, and version/env-name validation; CLI `--config` test exits 3 |
| Initialization and help do not require valid configuration; doctor reports config errors as 3 and other critical failures as 1 | User request — Requirements 7–8; supported initialization/repair combinations | Missing explicit target can be initialized; malformed config can be replaced with `init --force`; root and feature help work despite invalid config |
| Effective logging uses flag > `LEAN_WEAVE_LOG_LEVEL` > `LOG_LEVEL` > YAML > default; format uses flag > YAML > default | `RULES.md` — logging control; user request — structured logging and YAML configuration | Precedence matrix tests; logs stay on stderr and config output parses independently; environment overrides are not persisted |
| No LangGraph, MCP, ACP, Docker, workers, multi-agent routers, or code-editing agent; `feature` is help-only in this plan | User request — Constraints; `RULES.md` MVP exclusions; ADR-002 | `wl feature` and `wl feature --help` exit 0; no subcommand performs work; `tests/test_dependency_policy.py` rejects forbidden runtime packages |
| Full-mode branch/worktree creation is skipped because the user mandated the exact plan identifier; plan records `Branch: none` | User request — "Save the plan using the exact identifier"; `workflow.plan_id_format: slug` | Plan file is exactly `.ai-factory/plans/v1-02-project-foundation.md`; `git status` unchanged apart from plan artifact |

## Acceptance Criteria Mapping

| Acceptance criterion | Planned evidence |
|----------------------|------------------|
| Package installs successfully | `uv sync` and `uv build` succeed; built wheel installs into a temporary venv and exposes `wl`/`lean-weave` entry points |
| CLI commands work | `wl --help`, `wl init`, `wl config show`, `wl doctor`, `wl feature --help`, and `wl feature` exit codes verified with `CliRunner` and a manual smoke run |
| Configuration validation works | Loader tests cover valid, partial, malformed, unknown-key, bad-type, missing default/explicit paths, unsupported version, and unsafe `state_dir` |
| Domain models support serialization | `FeatureRequest`, `FeaturePlan`, `ImplementationTask`, `FeatureState`, `VerificationResult`, `TokenUsage` JSON round-trip tests |
| Tests pass offline | Prepared core and full-spike environments pass `uv run --offline --no-sync pytest` with the appropriate test selection under the socket guard, with no `DEEPSEEK_API_KEY` and with a dummy value |
| Foundation supports dependency injection | `AppContext` accepts override implementations of Protocols; tests inject fakes without monkeypatching module globals |

## Commit Plan
- **Commit 1** (after tasks 1-3): `feat: add packaging, structured logging, and config loading`
- **Commit 2** (after tasks 4-6): `feat: add domain models, typed contracts, and app context`
- **Commit 3** (after tasks 7-8): `feat: add wl CLI and offline test guard`
- **Commit 4** (after tasks 9-10): `docs: document the CLI foundation and verify offline acceptance`

## Tasks

### Phase 1: Package, logging, and configuration foundation

- [x] Task 1: Configure package metadata, dependencies, and console scripts
  Completion: `uv sync` and `uv build` passed; Typer 0.29.1, PyYAML 6.0.3, Pydantic 2.14.3. CLI script wiring is verified in Task 7.
  Update `pyproject.toml` without replacing the existing project:
  - Keep `name = "lean-weave"`, `requires-python = ">=3.12"`, `uv_build` backend, and existing spike optional groups.
  - Add runtime dependencies `typer>=0.12` and `PyYAML>=6.0` alongside `pydantic>=2.0`. Do not add LangGraph, MCP, Docker, HTTP, or LLM SDKs to runtime dependencies.
  - Add `wl = "lean_weave.cli:main"` to `[project.scripts]`; keep `lean-weave = "lean_weave:main"` as the canonical alias. Do not add an `aif` script.
  - Expose `__version__` in `src/lean_weave/__init__.py` (read from installed package metadata with a safe fallback). Leave `main()` unchanged in this task; Task 7 rewires it to `lean_weave.cli.main` after that module exists.
  - Keep `src/main.py` untouched and out of packaging.
  - Update `uv.lock` through `uv sync`; do not hand-edit it.

  LOGGING REQUIREMENTS:
  - This task adds no runtime logging and must not introduce import-time logging side effects.
  - `uv sync`/`uv build` output is the verification log; record resolved versions in the task completion note.
  - Format: `[Packaging.configure]` messages only if a helper script is added.
  - Control via `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`) for any Python helper.

  Files: `pyproject.toml`, `uv.lock`, `src/lean_weave/__init__.py`
  Depends on: none

- [x] Task 2: Implement structured logging with secret redaction
  Completion: core regression passed (17 tests), including both formatters, nested/argument/exception redaction, custom env names, idempotence, precedence, and silent schema imports.
  Create `src/lean_weave/logging_setup.py` using only the standard library:
  - `resolve_log_level(explicit: str | None = None) -> int` resolves explicit flag, then `LEAN_WEAVE_LOG_LEVEL`, then `LOG_LEVEL`, then `INFO`; invalid values log a `WARN` and fall back to `INFO` without raising.
  - This low-level resolver handles bootstrap logging only. After config loads, the CLI selects flag > `LEAN_WEAVE_LOG_LEVEL` > `LOG_LEVEL` > YAML > `INFO` and passes the selected value explicitly; format precedence is flag > YAML > `text`. Invalid selected levels warn and fall back to `INFO`, without echoing the rejected value.
  - `configure_logging(level: str | None = None, fmt: Literal["text", "json"] = "text", stream: IO[str] | None = None) -> None` configures a `lean_weave` root handler idempotently and leaves third-party loggers at `WARNING`.
  - `JsonFormatter` emits one JSON object per line with `timestamp`, `level`, `logger`, `message`, and optional `extra` fields; no secret values.
  - `SecretRedactionFilter` masks values of keys matching `api_key`, `token`, `secret`, `authorization`, `password`, and masks the current `DEEPSEEK_API_KEY` value if set.
  - Allow `configure_logging` to accept keyword-only `secret_env_names: Sequence[str] = ("DEEPSEEK_API_KEY",)`; include the configured `llm.api_key_env` after loading config. Resolve values only inside the redaction layer, never in config or `AppContext`. Redact nested mappings/sequences, interpolated message arguments, and exception text before either formatter emits them; test text and JSON output with default and custom env names.
  - Default the stream to stderr. Reconfiguration replaces only owned application handlers, avoids duplicate records, and does not redirect machine-readable command output.
  - `get_logger(name: str)` returns a namespaced logger.
  - Remove the import-time `logging.basicConfig` block **and** the module-level `log.info`/`log.debug` calls from `src/lean_weave/schemas/models.py`; keep `TRANSITIONS` as data. Importing a schema module must not emit log records.
  - Add a test that importing `lean_weave.schemas` emits no records and leaves the root logger unchanged.

  LOGGING REQUIREMENTS:
  - INFO: effective level and format at configuration time, without secret values.
  - DEBUG: which environment source supplied the level and whether redaction filter was installed.
  - WARN: invalid level value, fallback applied.
  - ERROR: never log full prompt or API key values; redact before formatting.
  - Format: `[logging.configure] level=<LEVEL> format=<text|json> source=<flag|LEAN_WEAVE_LOG_LEVEL|LOG_LEVEL|default>`
  - Control via `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`).

  Files: `src/lean_weave/logging_setup.py`, `src/lean_weave/schemas/models.py`, `tests/test_logging_setup.py`
  Depends on: 1

- [x] Task 3: Implement YAML configuration models, loader, init template, and safe dump
  Completion: core regression passed (46 tests), covering safe YAML errors, strict validation, explicit/default paths, init overwrite handling, symlink containment, and IO errors. Builder integration checks are scheduled with Task 6, which introduces that builder.
  Create `src/lean_weave/config/`:
  - `models.py`: Pydantic models with `extra="forbid"`:
    - `ProjectConfig`: `name: str = "lean-weave"`.
    - `LLMConfig`: `provider: Literal["deepseek"] = "deepseek"`, `model: str = "deepseek-flash"`, `base_url: str = "https://api.deepseek.com"`, `thinking: bool = False`, `api_key_env: str = "DEEPSEEK_API_KEY"`, `timeout_s: float > 0 = 60`.
    - `LoggingConfig`: `level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"`, `format: Literal["text", "json"] = "text"`.
    - `WorkflowConfig`: `state_dir: str = ".lean-weave"`, `max_retries: int >= 0 = 2`. Validate `state_dir` as a non-empty relative path with no `..` segments; absolute paths are rejected. The loader additionally resolves it against the project root and rejects any value that escapes the root.
    - `LLMConfig.api_key_env`: validate as an environment-variable identifier matching `^[A-Z_][A-Z0-9_]*$`.
    - `AppConfig`: `version: Literal[1] = 1`, `project`, `llm`, `logging`, `workflow`. Unsupported versions raise `ConfigError`.
  - `loader.py`:
    - `DEFAULT_CONFIG_PATH = Path(".lean-weave/config.yaml")`.
    - `load_config(path: Path | None = None, *, required: bool = False) -> LoadedConfig`, where `LoadedConfig` carries `config: AppConfig`, `source: Path | None`, and `warnings: list[str]`. Use `yaml.safe_load` only.
    - Path semantics: `path=None` first attempts `Path.cwd() / DEFAULT_CONFIG_PATH`; load it when present. Only a missing implicit default uses defaults and records a `WARN`. Callers supplying an explicit path use `required=True`; missing explicit files raise `ConfigError`. Malformed YAML, non-mapping root, unknown keys, and validation errors raise `ConfigError` with the file path and safe field error (no secret values).
    - Wrap read/decoding errors, directory targets, and path-resolution failures in safe `ConfigError` messages. Never stringify raw YAML parser errors or Pydantic errors containing input values; use field locations/error codes and YAML line/column positions only.
    - MVP project root is `Path.cwd()`; resolve `state_dir` against it and reject escapes. Document this explicitly for later root-discovery work.
    - Keep `LoadedConfig.config` and `dump_config` as file/default values; do not mutate them with environment overrides. The CLI applies effective logging overrides after loading, without persisting them.
    - `init_config(path: Path | None = None, force: bool = False) -> Path` creates parent directories, writes a commented default YAML template, refuses to overwrite unless `force=True`, refuses a directory target, and never writes secrets.
    - `dump_config(config: AppConfig) -> str` emits YAML with `llm.api_key_env` (an env var name) but no key value; add `api_key_present: bool` only in the CLI layer.
  - `__init__.py`: export `AppConfig`, `ConfigError`, `LoadedConfig`, `load_config`, `init_config`, `dump_config`, `DEFAULT_CONFIG_PATH`.
  - Create the base error hierarchy in `src/lean_weave/errors.py` (including `ConfigError`); Task 7 extends it with CLI exit-code mapping. Do not create two error hierarchies.
  - Add tests for: implicit missing default vs explicit `required=True`; `state_dir` absolute/`..`/escape rejection; unsupported `version`; bad `api_key_env`; `yaml.safe_load` behavior; and `init_config` parent creation, directory refusal, and `force`.
  - Also test an existing default file through `load_config()` and `build_app_context()`, ignored `.ai-factory/config.yaml`, symlinked state paths escaping the project root, unreadable/undecodable files, and malicious YAML/validation inputs containing secret-looking values. Assert errors and logs do not echo those values.

  LOGGING REQUIREMENTS:
  - INFO: config loaded from explicit source or defaults; effective log level.
  - DEBUG: keys present in YAML (names only), resolved path, env override application.
  - WARN: configured file missing while defaults are used; env override differs from file.
  - ERROR: YAML parse/validation failure with file path and field name only; never echo secret-looking values.
  - Format: `[Config.load] source=<path|defaults> path=<resolved>` and `[Config.init] path=<path> force=<bool>`
  - Control via `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`).

  Files: `src/lean_weave/config/__init__.py`, `src/lean_weave/config/models.py`, `src/lean_weave/config/loader.py`, `src/lean_weave/errors.py`, `tests/config/test_config_models.py`, `tests/config/test_config_loader.py`
  Depends on: 1, 2
<!-- Commit checkpoint: tasks 1-3 -->

### Phase 2: Domain models, typed contracts, and dependency injection

- [x] Task 4: Add `FeatureRequest` and Pydantic `TokenUsage`; preserve existing schema behavior
  Completion: 61 core tests passed, including JSON round-trips for all six domain models and unchanged v1-01 schema tests.
  Extend `src/lean_weave/schemas/models.py` and `src/lean_weave/schemas/__init__.py`:
  - `FeatureRequest`: `feature_id: str` (non-empty), `description: str` (non-empty), `title: str | None = None`, `created_at: datetime` defaulting to timezone-aware UTC, `metadata: dict[str, Any]` defaulting to empty. Forbid extra fields.
  - `TokenUsage`: Pydantic model with `prompt_tokens`, `completion_tokens`, `total_tokens`, `reasoning_tokens`, `prompt_cache_hit_tokens`, all non-negative ints defaulting to 0. Keep `TokenCounters` as the aggregate state counter with `llm_calls`.
  - Keep `FeaturePlan`, `ImplementationTask`, `FeatureState`, `VerificationResult`, `Stage`, `TaskStatus`, `VerificationStatus`, `SCHEMA_VERSION`, and `TRANSITIONS` unchanged in name and semantics; existing tests must pass without edits.
  - Export the two new models from `schemas/__init__.py`.
  - Add JSON round-trip and validation tests, including forbidden extra fields and negative-token rejection.

  LOGGING REQUIREMENTS:
  - No module-level logging side effects and no logging of model payloads or timestamps at import.
  - DEBUG: callers may log entity names/counts, never serialized state bodies or secrets.
  - ERROR: validation failures surface as Pydantic errors to the caller; do not duplicate them in logs.
  - Format: `[Schemas.extend] entity=<name> version=<SCHEMA_VERSION>` only at application startup if needed.
  - Control via `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`).

  Files: `src/lean_weave/schemas/models.py`, `src/lean_weave/schemas/__init__.py`, `tests/schemas/test_models_v102.py`
  Depends on: 2

- [x] Task 5: Tighten the existing Protocols to domain types without changing their names or methods
  Completion: 63 core tests passed; preserved exports and resume signature, exercised typed fakes and all supported decision shapes.
  Update `src/lean_weave/contracts/`:
  - Replace `Any` parameters/returns with `FeatureState` / `VerificationResult` / `FeatureRequest` where the v1-01 contract already defines them.
  - `StateStore.load(...) -> FeatureState`; `save(feature_state: FeatureState)`.
  - `ContextProvider.build(stage: StageName, feature_state: FeatureState) -> ContextBundle`.
  - `VerificationRunner.run(feature_state: FeatureState) -> VerificationResult`.
  - `WorkflowOrchestrator.run_feature(feature_id: str, request: str) -> FeatureState` and `resume(feature_id: str, decision: Any | None = None) -> FeatureState`; preserve the existing parameter types and optional default, tightening only the return values. Add a signature regression check and fake calls with omitted, mapping, and non-mapping decisions.
  - In `contracts/_types.py`, import `TokenUsage` from `lean_weave.schemas.models` and delete the duplicate TypedDict; keep `LLMResponse.usage` typed with the model.
  - Keep Protocol names, module paths, and `contracts.__all__` exports stable.
  - Add tests with minimal fake classes that structurally satisfy each Protocol and assert typed method calls.

  LOGGING REQUIREMENTS:
  - Protocol modules remain logging-free at import; no runtime behavior is added.
  - DEBUG: contract tests log each Protocol name checked and the fake used.
  - ERROR: type mismatches fail tests, not runtime logs.
  - Format: `[Contracts.verify] protocol=<name> ok=<bool>` in tests only.
  - Control via `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`).

  Files: `src/lean_weave/contracts/_types.py`, `src/lean_weave/contracts/context_provider.py`, `src/lean_weave/contracts/state_store.py`, `src/lean_weave/contracts/verification_runner.py`, `src/lean_weave/contracts/workflow_orchestrator.py`, `src/lean_weave/contracts/metrics_collector.py`, `tests/contracts/test_contracts_typed.py`
  Depends on: 4

- [x] Task 6: Add a small `AppContext` for dependency injection
  Completion: 65 core tests passed; explicit/default config behavior and fake StateStore injection verified through the builder.
  Create `src/lean_weave/app.py`:
  - `@dataclass AppContext` with `config: AppConfig`, `config_source: Path | None`, and optional slots typed by the existing Protocols: `llm_provider`, `context_provider`, `code_executor`, `verification_runner`, `state_store`, `metrics_collector`, `orchestrator`. Default all provider slots to `None`.
  - `build_app_context(config_path: Path | None = None, *, llm_provider: LLMProvider | None = None, context_provider: ContextProvider | None = None, code_executor: CodeExecutor | None = None, verification_runner: VerificationRunner | None = None, state_store: StateStore | None = None, metrics_collector: MetricsCollector | None = None, orchestrator: WorkflowOrchestrator | None = None) -> AppContext` loads config and assigns only the provided typed overrides.
  - Pass `required=config_path is not None` to `load_config`: implicit defaults may be absent, but explicit paths must exist. Test both cases through this builder, not just the loader.
  - No string-keyed `overrides` mapping and no `require(slot: str)` service locator; consumers read the typed attributes directly or raise `DependencyUnavailable` at the call site. Do not add a plugin registry or third-party DI framework.
  - Tests inject fake implementations through the typed keyword arguments and prove no monkeypatching of module globals is required.

  LOGGING REQUIREMENTS:
  - INFO: context built with config source and count of injected components.
  - DEBUG: names of injected slots only (never component internals or secrets).
  - SECURITY: never log config secret values; `DEEPSEEK_API_KEY` is not part of `AppContext`.
  - Format: `[AppContext.build] source=<path|defaults> injected=<n>`
  - Control via `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`).

  Files: `src/lean_weave/app.py`, `tests/test_app_context.py`
  Depends on: 3, 5
<!-- Commit checkpoint: tasks 4-6 -->

### Phase 3: CLI surface, error handling, and offline test guard

- [x] Task 7: Implement the Typer CLI, commands, and consistent exit codes
  Completion: 106 core tests passed, including command/help combinations, exported main error boundary, redaction, precedence, doctor probes/cleanup, and config repair. Typer is constrained to >=0.12,<0.17 with explicit Click >=8.1,<8.3 (resolved 0.16.1/8.2.1): latest Typer 0.29 vendors Click and no longer supports the requested Click exception boundary.
  Create `src/lean_weave/cli/`:
  - `errors.py` or extend `src/lean_weave/errors.py` with `ExitCode(IntEnum)`: `OK = 0`, `ERROR = 1`, `USAGE = 2`, `CONFIG = 3`. `LeanWeaveError` carries `exit_code`; `ConfigError` maps to 3; `DependencyUnavailable` maps to 1.
  - `app.py`: one `typer.Typer` application named for the `wl` command. Global options `--config PATH`, `--log-level`, `--log-format` on the root callback. Keep logging option defaults as `None` so explicit flags can be distinguished from YAML/default values.
  - Make startup command-aware: initialization receives its target path and flags without loading the file it is creating/replacing; root and nested help (including bare `feature`) bypass config loading. Ordinary config-consuming commands build `AppContext` and store it on `typer.Context.obj`. Do not require a valid file merely to display help.
  - For config-consuming commands, install safe bootstrap stderr logging, load config once, then reconfigure effective level using flag > `LEAN_WEAVE_LOG_LEVEL` > `LOG_LEVEL` > YAML > `INFO`, and format using flag > YAML > `text`; register the configured secret env name with redaction. Buffer/replay sanitized load diagnostics if necessary to avoid losing them before final configuration.
  - `commands/init.py`: `wl init [--force]` resolves the target from `--config` or the default `.lean-weave/config.yaml`, creates parent directories, writes the file, and prints the path. Existing file without `--force` exits 1; a directory target exits 1; success exits 0.
  - `commands/config.py`: `wl config show [--format yaml|json]` prints the resolved config and `api_key_present: true|false` only; never prints the key. An explicitly supplied `--config` that is missing or invalid exits 3; the implicit default may fall back to defaults with a WARN.
  - `commands/doctor.py`: `wl doctor` runs offline checks (Python >= 3.12, package import/version, config load, configured state-directory writability, git presence informational, configured secret env presence informational). Config failures exit 3; other critical failures exit 1; otherwise exit 0. Package version lookup falls back to `lean_weave.__version__` when distribution metadata is unavailable.
  - Doctor never calls the network or creates missing directories. Resolve `workflow.state_dir` against the project root using loader containment rules. When absent, check its nearest existing parent and report WARN; when present, reject file targets and probe writability using a temporary file removed in `finally`. Test default/custom/nested paths, permission failures, file targets, and cleanup on failure.
  - `commands/feature.py`: create a `feature` Typer sub-app with `invoke_without_command=True`; its callback prints help and exits 0, so both `wl feature` and `wl feature --help` work. Register no working feature subcommands in this plan. Do not implement planning, editing, or LLM execution.
  - `src/lean_weave/cli/__init__.py`: export `app` and `main(argv: Sequence[str] | None = None) -> None`. `main()` invokes `app(args=..., standalone_mode=False)`, catches `LeanWeaveError`, `click.ClickException`, and `click.UsageError`, prints a safe message to stderr, maps the error to `ExitCode`, and raises `SystemExit`. Typer/Click exceptions must not produce raw tracebacks.
  - `src/lean_weave/__init__.py`: `main()` delegates to `lean_weave.cli.main`; do not add CLI logic to `src/main.py`.
  - Add `CliRunner` tests covering help for every command, init create/refuse/force/directory-target, config show redaction and formats, explicit missing `--config` (exit 3), doctor pass/fail paths with monkeypatched environment, `wl feature` and `wl feature --help`, unknown command (exit 2), and config error (exit 3). Assert that failures do not leak tracebacks or the API key value.
  - Add combination tests: `wl --config new.yaml init` succeeds; malformed existing config permits `init --force`; root/command/feature help and bare `feature` succeed with missing or malformed explicit config. `config show` still exits 3 for the same invalid sources.
  - Check `api_key_present` against `config.llm.api_key_env`, not a hard-coded name. Assert stdout parses as YAML/JSON with INFO/DEBUG logs on stderr, test logging precedence across flags/env/YAML, and exercise exported `main(argv)` directly as well as `CliRunner` so the console error boundary is actually covered.
  - Do not add `aif` as a script or command name; `wl` is the executable per the user's follow-up.

  LOGGING REQUIREMENTS:
  - INFO: command name, sanitized arguments, config source, and final exit code.
  - DEBUG: AppContext slots used, doctor check names, redaction applied.
  - WARN: missing optional configured secret env variable (default `DEEPSEEK_API_KEY`), missing git, config file absent (defaults used).
  - ERROR: failures with safe message and exit code; never log full configs, prompts, or secret values.
  - Format: `[CLI.<command>] message {arg=..., exit_code=...}`
  - Control via `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`) and the `--log-level` flag.

  Files: `src/lean_weave/cli/__init__.py`, `src/lean_weave/cli/app.py`, `src/lean_weave/cli/commands/__init__.py`, `src/lean_weave/cli/commands/init.py`, `src/lean_weave/cli/commands/config.py`, `src/lean_weave/cli/commands/doctor.py`, `src/lean_weave/cli/commands/feature.py`, `src/lean_weave/errors.py`, `src/lean_weave/__init__.py`, `tests/cli/test_cli.py`, `tests/cli/test_doctor.py`
  Depends on: 2, 3, 6

- [x] Task 8: Enforce offline tests and forbidden-dependency policy
  Completion: core regression passed (13 tests), including IPv4/IPv6 connect/connect_ex, create_connection blocking, and AF_UNIX compatibility.
  Execute this task after Task 1, before config/CLI tests are introduced, despite its phase grouping. Create `tests/conftest.py`:
  - Add an autouse fixture that blocks `socket.socket.connect`, `socket.socket.connect_ex`, and `socket.create_connection` by raising a clear `RuntimeError` mentioning the `network` marker.
  - Allow an explicit opt-in only when the test carries `@pytest.mark.network`; no test in this plan may use that marker.
  - Block only network sockets; local subprocess execution used by tests remains allowed.
  - Reject AF_INET and AF_INET6 connections while preserving AF_UNIX sockets; wrap original methods rather than rejecting every socket family. Test IPv4 and IPv6 blocking and Unix-domain socket compatibility without external connections.
  - This in-process fixture does not sandbox child processes or collection-time imports. Keep subprocess smoke commands limited to known offline product paths; do not claim their sockets are guarded by pytest monkeypatches.
  - Register the `network` marker in `pyproject.toml` under `[tool.pytest.ini_options] markers`.
  Add `tests/test_dependency_policy.py`:
  - Parse `pyproject.toml` with `tomllib` and assert runtime `project.dependencies` excludes `langgraph`, `mcp`, `docker`, `openai`, and `mcp-*`/`docker-*` distributions.
  - Assert spike-only packages stay in optional/dependency groups and are not runtime dependencies.
  - Add a test that the socket guard raises on a direct connection attempt.

  LOGGING REQUIREMENTS:
  - INFO: offline guard active and network marker registered.
  - DEBUG: each blocked connection attempt (test id only, no payload).
  - ERROR: dependency-policy violations listing the offending package names.
  - Format: `[Tests.offline] guard=<active|bypassed> test=<id>`
  - Control via `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`).

  Files: `tests/conftest.py`, `tests/test_dependency_policy.py`, `pyproject.toml`
  Depends on: 1
<!-- Commit checkpoint: tasks 7-8 -->

### Phase 4: Documentation and offline acceptance

- [x] Task 9: Document CLI, configuration, logging, errors, and injection
  Completion: README/getting-started updated; CLI reference added; architecture navigation updated. Reviewed command accuracy, links/navigation, offline-environment distinction, and exit codes 0/1/2/3.
  Update developer documentation:
  - `README.md`: replace the hello-only stub examples with `uv run wl ...`, list `wl` and `lean-weave` aliases, and point to the new CLI docs.
  - `docs/getting-started.md`: install/sync, `uv run wl init`, `wl config show`, `wl doctor`, `wl feature --help`, logging environment variables, offline test command, and the `network` marker opt-in.
  - Document two prepared environments: core (`uv sync`, then `uv run --offline --no-sync pytest --ignore=tests/spikes`) and full architecture regression (`uv sync --group spikes`, then `uv run --offline --no-sync pytest`). Existing spike imports require optional packages; do not imply plain sync supports full-suite collection. Dependency acquisition may need a package index/cache; offline test execution must not.
  - `docs/cli.md` (new): command reference, `.lean-weave/config.yaml` schema with defaults, explicit-vs-implicit config path behavior, exit-code table (0/1/2/3), logging formats and redaction rules, and `AppContext` injection example for tests.
  - Include logging precedence, stderr/stdout separation, configured secret-env presence/redaction, init/help config bypass, doctor's config-error code and configured-directory behavior, and the in-process scope of the socket guard.
  - State explicitly that `aif` is not an executable in this project; `wl` is the CLI alias and `lean_weave:main` remains canonical.
  - Include a short "not implemented in v1-02" list: LLM calls, code editing, orchestration execution, LangGraph, MCP/ACP/Docker/workers.

  LOGGING REQUIREMENTS:
  - Documentation task: INFO listing files updated and the documented exit-code values.
  - No code logging changes; examples must not include real secrets.
  - Format: `[Docs.update] files=<count> exit_codes=0,1,2,3`
  - Control via `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`) for any doc-check helper.

  Files: `README.md`, `docs/getting-started.md`, `docs/cli.md`
  Depends on: 7, 8

- [x] Task 10: Verify offline acceptance and fix regressions
  Completion: clean core `uv sync` and `uv build` passed; core offline suite passed with key unset/dummy (110 tests each). Explicit `uv sync --group spikes` prepared the full suite, which passed offline with key unset/dummy (118 tests each). Wheel/sdist inspected; built wheel installed in a separate venv and both executables smoke-tested outside the source tree, including init refusal/repair, invalid-config help bypass, doctor custom state paths, custom key presence, and parseable YAML/JSON output with logging. Wheel dependency installation needed index access after an offline cache miss; runtime verification used installed executables without dependency resolution. `src/main.py` is unchanged; dependency policy and `git diff --check` pass. No commits created (user skipped all checkpoints). Thin CLI roadmap milestone remains open because orchestrator wiring/start/resume/status are outside this foundation plan.
  Run and record the acceptance checks (no report artifact or LLM calls). Separate dependency preparation, which may access package indexes, from offline runtime verification:
  - In a clean environment, `uv sync` and `uv build` succeed (wheel/sdist contains `lean_weave` and the `wl`/`lean-weave` scripts). Run core tests with `uv run --offline --no-sync pytest --ignore=tests/spikes`.
  - Prepare optional spike dependencies with `uv sync --group spikes`; run the full regression suite with `uv run --offline --no-sync pytest`. Do not silently rely on preinstalled optional libraries.
  - Run both test selections with `DEEPSEEK_API_KEY` unset and with `DEEPSEEK_API_KEY=dummy`; confirm in-process network attempts are blocked. Include a custom `llm.api_key_env` redaction/presence test.
  - Install the built wheel into a temporary virtual environment, inspect `entry_points.txt`, and run `wl --help` and `lean-weave --help` from outside the source tree.
  - CLI smoke in a temporary directory: `wl --help`, `wl init`, `wl config show`, `wl doctor`, `wl feature --help`, `wl feature` (no args), plus `wl init` twice to confirm the non-zero overwrite refusal.
  - Also smoke explicit-path creation, malformed-config repair/help bypass, doctor with a custom state directory, and YAML/JSON config output parsing with logging enabled. Use installed executables directly for runtime smoke checks; no dependency resolution occurs during these commands.
  - Confirm `src/main.py` is unchanged and no forbidden dependency (`langgraph`, `mcp`, `docker`, `openai`) is in runtime dependencies.
  - Fix any failures found; do not mark acceptance complete with failing tests.

  LOGGING REQUIREMENTS:
  - INFO: each acceptance command and its exit code.
  - DEBUG: temporary directory path and resolved config source.
  - ERROR: failing command output with secrets redacted; failing tests block completion.
  - Format: `[Acceptance.run] command=<name> exit_code=<n>`
  - Control via `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`).

  Files: no new artifacts expected; fixes may touch any files from tasks 1-9
  Depends on: 1, 2, 3, 4, 5, 6, 7, 8, 9
<!-- Commit checkpoint: tasks 9-10 -->
