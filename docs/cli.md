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
| `wl context search QUERY [--path PATH] [--limit N] [--symbol NAME] [--json]` | Literal snapshot search or exact symbol lookup; offline |
| `wl context search QUERY --task-aware [--task-kind KIND] [--kind all\|file\|symbol] [--top-k N] [--json]` | Opt-in hybrid task-aware retrieval (see [context-retrieval.md](context-retrieval.md)) |
| `wl context rank QUERY [--task-kind KIND] [--kind all\|file\|symbol] [--top-k N] [--json]` | Rank task-relevant files/definitions with scores and cost |
| `wl context explain QUERY [--task-kind KIND] [--kind all\|file\|symbol] [--top-k N] [--json]` | Explain ranked hits with reasons, dependencies, and diagnostics |
| `wl context build TASK [--path PATH ...] [--level map\|symbols\|ranges\|full] [--reason TEXT] [--json]` | Build a budgeted, provenance-bearing context pack |
| `wl context build QUERY --packed [--pack-level l0\|l1] [--json]` | V2 token-aware packed build (see [context-packing.md](context-packing.md)) |
| `wl context inspect PATH [--symbol NAME \| --lines START:END] [--json]` | Safe file metadata or one admitted range |
| `wl context inspect QUERY --packed [--pack-level l0\|l1] [--json]` | Packed provenance view without source bodies |
| `wl context budget QUERY [--pack-level l0\|l1] [--json]` | Numeric packed limits and composition only |
| `wl cache status` | Inspect the opt-in context cache without opening source |
| `wl cache stats` | Show cache hit/miss/rebuild/invalidation counters |
| `wl cache clear --yes` | Remove this checkout's context-cache data |
| `wl cache rebuild` | Seed a fresh cache from one strict scan (see [context-cache.md](context-cache.md)) |
| `wl llm check` | Opt-in DeepSeek smoke test: one small fixed request plus safe usage output |
| `wl feature DESCRIPTION (--auto \| --review) [--worktree PATH]` | Start a feature inside an isolated linked worktree; `--worktree` adopts an existing clean checkout |
| `wl status FEATURE` | Read-only workflow status, branch, changes, verification, usage, and next action |
| `wl result FEATURE` | Read-only result view including bounded diff summary and terminal/archived state |
| `wl resume FEATURE [--approve TOKEN]` | Resume an interrupted run or accept the outstanding review gate |
| `wl cancel FEATURE` | Finalize an idle run or publish a durable cancellation request for an active run |
| `wl worktree inspect FEATURE` | Read-only worktree routing and cleanliness inspection |
| `wl worktree cleanup FEATURE --confirm` | Archive recognized state and remove an eligible clean managed worktree; never deletes the branch |
| `wl stats [--json]` | Read-only checkout-wide feature usage/context statistics |
| `wl stats feature FEATURE [--json]` | Read-only feature, stage, task, model and strategy breakdown |
| `wl benchmark run --suite workflow\|retrieval\|all --output FILE [--case ID] [--variant NAME] [--repeats N]` | Deterministic offline suite; live flags add explicit token/cost/deadline/pricing limits |
| `wl benchmark compare BASELINE CANDIDATE --axis strategy\|map\|budget\|cache\|revision [--thresholds FILE] [--json]` | Paired comparison with optional regression gates |
| `wl benchmark report RESULT [--output FILE]` | Markdown report from a versioned run result |
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
  max_retries: 2
  max_output_tokens: 1024
logging:
  level: INFO
  format: text
workflow:
  state_dir: .lean-weave
  max_retries: 2
  feature_token_budget: 20000
context:
  stage_prompt_tokens_max: 6000
  per_file_chars_max: 12000
  total_code_chars_max: 40000
  digest_chars_max: 2000
  retry_excerpt_chars_max: 8000
  candidate_bytes_max: 131072
  scan_bytes_max: 8388608
  visited_entries_max: 20000
  search_hits_max: 100
  recent_commits_max: 10
  exclusions: []
  cache:
    enabled: false
    max_bytes: 16777216
    max_entries: 4096
tools:
  read_allow: []
  write_allow: []
  excludes: []
  scan_bytes_max: 8388608
  file_bytes_max: 131072
  visited_entries_max: 20000
  result_chars_max: 12000
  range_lines_max: 400
  search_hits_max: 50
  patch_bytes_max: 65536
  patch_hunks_max: 32
  checks_max: 8
  runtime_s_max: 30.0
  capture_bytes_max: 65536
  diagnostics_max: 20
  checks: []
```

Partial mappings merge with model defaults. All sections reject unknown keys.
`provider` supports only `deepseek`; `timeout_s` must be finite and positive;
`llm.max_retries` is 0–5 extra gateway transport attempts (default 2);
`llm.max_output_tokens` must be positive (default 1024).
`workflow.max_retries` bounds repair attempts after attributable verification
failures (default 2). `workflow.feature_token_budget` is a positive
total-processed token budget per feature (default 20000). Logging levels are
DEBUG, INFO, WARNING, ERROR, CRITICAL; formats are text or json.
`version` supports only 1. `api_key_env` must match `^[A-Z_][A-Z0-9_]*$`.
`base_url` must be an absolute HTTPS URL with a host, no user information, and
no query or fragment; an operator-selected path is preserved. The configured
path must not contain the API key value.
`state_dir` must be non-empty and relative, with no `..` segments; resolved
symlinks must stay inside the project root.

`context.*` limits are strict, positive integers. `exclusions` are additive
project-relative gitwildmatch patterns; they cannot override hard denials.
`wl config show` prints the numeric limits and `exclusion_count`, never the raw
exclusion strings. Per-command search limits may only tighten
`context.search_hits_max`.

`tools` stores read/write allowlists, additive excludes, numeric budgets, and
optional check definitions. Those lists do not authorize writes or process
spawns; a caller must build an approval snapshot and opt in. `config show`
prints `read_allow_count`, `write_allow_count`, `exclude_count`, `check_count`,
and the numeric bounds. It does not print allowlists, exclude patterns, check
argv, or executable paths. There is no CLI command for the code tools.
See [Standalone code tools](code-tools.md).

Secrets are environment-only. Set the variable named by `llm.api_key_env`
through your environment/secret manager; never put a key in YAML.
`config show` reports only whether that variable has a non-empty value.
It does not apply logging environment overrides to the printed configuration.
Known secret values embedded in configuration strings are redacted from both
YAML and JSON output.

## Deterministic context preview

`wl context` and `lean-weave context` run entirely offline. They make no LLM
calls, write no index or pack, do not require `DEEPSEEK_API_KEY`, and do not
invoke the orchestrator or StateStore. They expose the retrieval service for
manual inspection; the feature workflow builds its own stage-scoped packs
through the same `ContextProvider` during `wl feature` / `wl resume`.

```bash
# Case-sensitive literal search over eligible canonical snapshots
uv run wl context search "create_llm_provider" --json

# Exact symbol lookup by name and optional file
uv run wl context search helper --symbol helper --path pkg/core.py --json

# Opt-in hybrid task-aware retrieval (see context-retrieval.md)
uv run wl context search "fix login" --task-aware --json
uv run wl context rank "fix login" --task-kind bugfix --top-k 5 --json
uv run wl context explain "fix login" --task-kind bugfix

# Default pack: compact map plus selected signatures
uv run wl context build "wire the provider seam" --path src/lean_weave/app.py --json

# Complete AST spans for selected symbols
uv run wl context build "wire the provider seam" --path src/lean_weave/app.py --level ranges

# Full source only with one explicit path and a justification
uv run wl context build "review the seam" --path src/lean_weave/app.py \
  --level full --reason "the whole module is the review unit"

# Safe metadata, one symbol, or one explicitly labelled partial excerpt
uv run wl context inspect src/lean_weave/app.py --json
uv run wl context inspect src/lean_weave/app.py --symbol create_llm_provider
uv run wl context inspect src/lean_weave/app.py --lines 75:95

# Opt-in V2 token-aware packing (see context-packing.md)
uv run wl context build "wire the provider seam" --packed --json
uv run wl context inspect "wire the provider seam" --packed --json
uv run wl context budget "wire the provider seam" --json
```

Legacy invocation is the default. `--packed` / `--pack-level` opt into V2;
`--legacy` always restores the V1 view. Conflicting explicit signals exit `2`.
Setting `context.packing.cli_default: true` makes bare `build QUERY` and
`inspect QUERY` packed without changing the other commands.

### Expansion and provenance

| Level | Adds | Notes |
|-------|------|-------|
| `map` | Compact path/module/symbol-count map | Safe admitted paths only; no source bodies in the prompt |
| `symbols` | Selected signatures | Default; no full files |
| `ranges` | Complete AST `class` / `def` spans | Decorators included; non-Python explicit paths use the complete file |
| `full` | One complete file | Requires exactly one `--path` and `--reason` |

Every admitted excerpt carries its project-relative path, original 1-based
inclusive line range, and snapshot digest. Identical code is stored once with
alternate source ranges as provenance. A manually selected `--lines` interval is
labelled `partial`; automatic ranges are complete AST units. Omissions are
diagnostic only and are not rendered into the prompt.

### Budgets

Context is admitted as whole units. Required framing (system instructions, task,
trusted constraints, approved digests) is checked first; optional map, signature,
range, and full-file units are added only if the complete serialized message pair
still fits `stage_prompt_tokens_max`. Per-file, total-code, and retry excerpt
character caps also apply. An over-budget range keeps its previously admitted
signature; an over-budget full file falls back to lower detail and reports a
stable omission code. Code is never cut to hit a limit. If the required envelope
itself cannot fit, the command fails as an operational error.

The prompt estimate uses the conservative local estimator in
`metrics/budget.py` over the complete rendered message list. It is not measured
provider usage and does not spend the feature budget enforced by the DeepSeek
gateway.

### Eligibility, search, and Git

- Root is the canonical current working directory; no ancestor discovery.
- Nested `.gitignore` files apply with negation and excluded-parent semantics,
  with or without a Git repository. Configured exclusions are additive.
- Hard denials always win: VCS metadata, `.lean-weave`/configured state, skill
  trees and AI Factory working artifacts, credentials/keys/env files,
  dependency/build/cache/coverage directories, lock/generated files, binary,
  undecodable, oversized, non-regular, and symlink paths.
- Credential patterns quarantine a candidate before its path, source, or
  signature is exposed. Repository maps contain only safely admitted paths;
  credential-quarantined files, secret-like names, oversized/binary/unreadable
  files, and hard-denied paths are omitted. This is a bounded high-confidence
  filter, not a universal secret detector; the same filter rejects secret-like
  task text and explicit paths.
- Stage-scoped builds limit the repository map to the stage's eligible scope
  (task paths, one-hop imports, and enabled matching tests), never the whole
  repository.
- Search uses `rg --no-config --fixed-strings --json` over stdin only; `rg`
  never opens repository paths. `--path` filters the eligible snapshot set
  before `--limit` is applied. Missing `rg`, timeout, output overflow, or a
  malformed backend result discards partial output and falls back to Python
  substring matching over the same canonical lines.
- Git status and recent path/status summaries are optional preview-only recency
  signals. Uncommitted changes outrank committed positions. Git configuration
  is treated as untrusted; unsafe filter/diff configuration disables the
  signal. Diff bodies, commit messages, history content, and external helpers
  are never read or run.

### Context exit codes

| Code | Meaning |
|------|---------|
| 0 | Success, including zero search hits and omission notices |
| 1 | Operational failure: forbidden/unavailable path, unsafe input, required budget overflow |
| 2 | Invalid selector, conflicting flags, bad line bounds, or oversized `--limit` |
| 3 | Configuration failure |

`--json` emits one stable JSON payload on stdout. Human output escapes terminal
control characters without rewriting JSON source text.

## LLM gateway

The production gateway is `lean_weave.providers.deepseek.DeepSeekProvider`,
using the locked OpenAI-compatible SDK against DeepSeek. It is constructed only
on demand by `lean_weave.app.create_llm_provider`; help, init, config show,
doctor and feature commands do not create a client, require a key, open the
ledger, or touch the network.

Supported MVP combinations:

| Mode | Behavior |
|------|----------|
| Plain completion | Caller supplies messages; no implicit transcript or system prompt is added |
| Thinking | `extra_body.thinking` is sent explicitly as `enabled` or `disabled`; reasoning effort requires thinking |
| Tools | Calls are returned, never executed. Thinking-plus-tools replay requires prior assistant `reasoning_content` and matching tool result IDs |
| JSON object | `response_format: {type: json_object}` plus a caller JSON instruction; optional local Pydantic validation |
| Tools + JSON | Rejected; the modes are separate in MVP |
| Other JSON schemas / `/beta` | Rejected |

Only the explicitly configured model and the documented DeepSeek capability
set are accepted. Requests cannot raise the configured output ceiling.
Malformed replay, unsupported combinations and schema validators outside JSON
mode are rejected before any send.

Retries are bounded by `llm.max_retries` (0–5). Only timeouts, connection
failures, HTTP 429, HTTP 408, transient upstream 5xx, and the documented blank
JSON response are retried, with bounded backoff and a clamped `Retry-After`.
Authentication failures, unsupported requests, other permanent 4xx responses,
schema failures, and budget/ledger failures are never retried. The SDK client is
constructed with its own retries disabled and redirect following disabled.

### Usage ledger and budgets

Accounting lives at `<state_dir>/metrics/llm-usage.jsonl` (default
`.lean-weave/metrics/llm-usage.jsonl`). It is an append-only, versioned JSONL
journal of reservation and terminal events. It stores counters, attribution,
model names, status, duration, retry ordinal, and safe error codes only — never
prompts, tool payloads, reasoning text, headers, or full responses.

Before **each** send, including retries, the gateway reserves estimated input
tokens plus the effective output cap. Admission is atomic across collectors for
the same canonical journal through a process-shared file lock. A denied
reservation sends nothing. Reservations left by a crash remain charged and are
reported separately as unresolved.

Measured provider counters are kept separate from local estimates. Missing
optional cache/reasoning details do not make core usage incomplete. If the
three core counters are valid and `total = input + output`, the reported total
is the measured charge; otherwise the greater of the reservation and a known
lower bound is charged conservatively. Cache-hit and reasoning counts are
subsets of reported usage, never additions or deductions. An explicit zero is
preserved; it is not treated as missing, and cache fallbacks are used only when
the preferred count is unavailable, not when it is zero.

The preflight estimate is intentionally conservative but local: it cannot
guarantee exact provider billing when the provider has not yet reported usage.
The compatibility `totals(feature_id)` view contains measured counters only;
the richer `summary(feature_id)` view exposes presence/coverage, stage/model
breakdowns, retry sends, elapsed attempt durations, unresolved reservations,
and measured versus estimated budget charges.

Only one logical writer set may target a journal. Writers are serialized by the
same lock; lock or journal unavailability fails closed, and malformed,
conflicting, orphaned, or partially written records are rejected rather than
resetting spend.

## Connection check

`wl llm check` is the only ordinary command that dials DeepSeek. It sends one
fixed, non-thinking, non-tool request capped at
`min(llm.max_output_tokens, 32)`, forces zero gateway retries, uses the selected
model and timeout, and attributes usage to the reserved probe feature. It does
not accept prompts or tool input.

```bash
export DEEPSEEK_API_KEY=...   # environment only
uv run wl llm check
```

It prints only a safe status, model name, and reported usage counts (or
`usage=unavailable`); reply text is suppressed. Exit codes are 0 for success,
1 for operational failure (including a missing key or exhausted budget), 3 for
configuration failure, and 2 for argument errors. This is an opt-in,
potentially billable smoke test; doctor and feature commands never invoke it.

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
| 0 | Success, help, or a rendered typed feature outcome (`success`, `failed`, `budget_exhausted`, `interrupted`, `cancelled`) |
| 1 | Operational failure, init refusal, unavailable dependency, doctor critical failure |
| 2 | Usage/parser error |
| 3 | Configuration read/parse/validation failure (including invalid `--auto`/`--review` combinations and `state_dir_drift`) |

Feature `status` / `result` / `resume` / `cancel` print stable `key=value` fields
(`feature_id`, `status`, `stage`, `reason`, `verification`, `usage`,
`next_action` as applicable). They are not a JSON API.

Expected failures print safe errors without raw tracebacks. See
[Benchmarks](benchmarks.md) for offline/live measurement tooling.

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

`create_llm_provider(context)` constructs the DeepSeek gateway on demand.
An injected `llm_provider` always wins. Otherwise accounting comes from an
injected attempt journal, an atomic `metrics_collector` slot, or the default
JSONL ledger. A legacy collector that only implements `record_usage`/`totals`
fails with `DependencyUnavailable` instead of silently bypassing budgets.

## MVP 1 exclusions

`wl feature` runs the bounded plan → implement → verify → repair loop with
worktree isolation, approvals, and deterministic checks. Context preview remains
a separate offline retrieval command. There is no LangGraph runtime, generic
multi-provider router, MCP/ACP client, Docker orchestration, background worker,
or nested coding-agent session. Architecture spikes under `spikes/` stay optional
validation tools, not the product runner. See [MVP 1 readiness](mvp1-readiness.md).

## See Also

- [Getting Started](getting-started.md) — installation and offline test environments
- [Architecture](architecture.md) — component boundaries and decisions
- [Benchmarks](benchmarks.md) — fixture corpus and token baseline tooling
- [Context cache](context-cache.md) — opt-in cache commands and recovery
- [MVP 1 readiness](mvp1-readiness.md) — offline/live/safety verdicts
