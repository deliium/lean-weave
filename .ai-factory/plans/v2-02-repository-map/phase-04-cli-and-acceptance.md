# Phase 4: Offline CLI and acceptance

Plan: [index.md](index.md)  
Tasks: 6–7  
Depends on: Phase 3 / Tasks 4–5

## Objective

Expose all four map queries through both installed entry points and verify the feature against the existing offline repository and workflow fixtures.

## Current-Code Evidence

| Path | Symbols | Why it matters |
|---|---|---|
| `src/lean_weave/cli/app.py` | `app`, `SafeGroup`, `startup` | Register the group alongside `index_app`; help must stay lazy. |
| `src/lean_weave/cli/commands/index.py` | `_fail`, `_HINTS`, `status_command` | Existing safe exit and recovery guidance; don't surface raw SQLite exceptions. |
| `src/lean_weave/cli/runtime.py` | `command_context` | Existing config and logging setup for real commands only. |
| `tests/code_index/test_offline_e2e.py` | `fixture_repo`, `test_real_fixture_build_lookup_and_range`, `test_warm_fixture_prompt_matches_cold` | Representative `bug_fix` fixture and known source-range and prompt checks. |
| `src/lean_weave/context/render.py`, `src/lean_weave/context/models.py`, `benchmarks/fixtures/trees/multi_file_feature/src/app.py` | `SymbolRef` import/definition; `from src.config import VERSION` | Use real source for a resolved reference and a counterexample to inferred `src.` aliases. |
| `tests/cli/test_feature_offline_e2e.py` | offline feature acceptance | Run existing managed-worktree and mocked gateway flows without changes. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `src/lean_weave/cli/commands/map.py`, `src/lean_weave/cli/app.py` | create, modify | Thin command group and registration. |
| `tests/cli/test_map.py`, `tests/code_index/test_offline_e2e.py` | create, extend | CLI/error/laziness coverage and representative end-to-end case. |
| `README.md`, `docs/getting-started.md`, `docs/code-index.md`, `docs/architecture.md`, `docs/repository-map.md` | modify, create | Mandatory docs checkpoint and operator reference. |

## Task 6: Expose offline map commands

### Intent

Make the shared map API accessible without creating another CLI, touching workflow, or building an index implicitly.

### Implementation Steps

1. In `cli/commands/map.py` define `map_app = typer.Typer(no_args_is_help=True, ...)` and four thin handlers. Register it in `cli/app.py` under `map`.
2. Route `show`, `module <module>`, `dependencies <module>`, and `references <symbol>` to the corresponding Task 4 methods via `create_repository_map` (Task 5), with workspace root `Path.cwd()`. Run selector validation before loading the snapshot. Accept configured limits only; don't invent CLI config overrides. Use each result's exact rendered text, with no extra success banner or unbudgeted newline.
3. Add a shared command boundary mapping map and CodeIndex failures to fixed safe codes, using the index recovery guidance (extract a shared hint function only if this preserves current index output byte-for-byte). Do not invoke `index build`, `index update`, Git, LLM, or approved tools.

### Required Interfaces and Contracts

- Invalid selector syntax: stderr, exit 2, no stdout; unknown or ambiguous selector: stderr, exit 1, no stdout. A resolved selector yielding zero evidence and `show` on a valid empty index both exit 0 with empty stdout. Selector commands on an empty index still report `not_found`. Import-edge ambiguity is data, not a CLI error. Budget exhaustion is success with truncation as defined in Task 4 (including the one-byte marker).
- Missing index → `wl index build`; stale → `wl index update`; busy/unstable → retry when stable; corrupt/incompatible/recovery-required → existing `wl index clear --yes` then `wl index build` guidance. Never print the selector itself in errors or logs.

### Requirement Evidence

- Original Request CLI, token efficiency and requirements 5–7, 10–13; reconciliation CLI lifecycle and budget rules. Verify `build → show → edit → stale failure → update → changed map` and unchanged cache on reads.

### Error Handling and Logging

- `DEBUG [CLI.map]` view, emitted row count, estimated tokens, truncated flag; `INFO` only for safe health/status if needed; `WARNING` for operational refusal with code only. No contents, absolute paths, user symbols, or raw exceptions. Diagnostics go to stderr via existing logging.

### Tests

- `tests/cli/test_map.py`: use `CliRunner`, temporary cwd and owner-only external `XDG_CACHE_HOME`. Exercise all commands, alias/qualified selector semantics, invalid vs unknown/ambiguous input, empty output, exact budget/newline, stale and missing refusal, recovery-code mapping, configured limits, no side effects on checkout/DB, and help without config or factory creation. Add a gateway-construction tripwire on a successful map command.

### Acceptance Criteria

- All four queries are visible in help and honor existing config/error dispatch; map commands are read-only and offline.

### Verification

- `uv run --locked pytest tests/cli/test_map.py tests/cli/test_index.py` — map contracts pass; index CLI behavior stays unchanged.

## Task 7: Verify and document repository maps

### Intent

Prove end-to-end traceability and existing feature workflow compatibility; document how to operate and interpret the map.

### Implementation Steps

1. Extend `tests/code_index/test_offline_e2e.py` with a `bug_fix` materialized-checkout test, using the manifest's `src/calc.py` for definition provenance. Build the existing index; assert `show` surfaces `calc.add`, and query `calc` / `calc.add` through the CLI. Compare the emitted definition location to both `CodeIndex.lookup` and the decoded file range. Edit the file by inserting a blank line before the definition, require stale refusal without old stdout, explicitly update the index, then check the new range. Compare bytes across identical invocations before the edit.
2. Add two relationship checks in the same test file. Copy only the existing `src/lean_weave/context/render.py` and `src/lean_weave/context/models.py` into a temporary checkout at those relative paths without importing them. Use an explicit `map_rows_max`/`map_tokens_max` config (500/8192) and external cache; drive `wl index build`, `wl map dependencies lean_weave.context.models` and `wl map references lean_weave.context.models.SymbolRef`. Assert the incoming `render.py` import span and definition span match the checked files, and run `wl context build --level map --indexed-map --path src/lean_weave/context/models.py --json` to confirm only that file is emitted. Separately materialize the unchanged `multi_file_feature` fixture and assert `dependencies app` reports the line-1 `src.config` import as `unresolved/not_indexed` even though `module config` succeeds. Do not copy from or alter fixture cache directories; use the existing materializer.
3. Run focused and full locked suites, including the existing warm/cold fixture prompt test and CLI workflow acceptance. Verify no workflow files, runtime dependencies, product state, benchmark fixtures, or LangGraph integration changed. Run `uv run --locked wl map --help` and `uv run --locked lean-weave map --help`.
4. Complete the mandatory `/aif-docs` checkpoint: add `docs/repository-map.md` with exact commands, settings/defaults, token-estimate semantics, truncation markers, examples and failure/remediation table. Link it from `README.md`, `docs/getting-started.md` and `docs/code-index.md`; document composition, explicit context opt-in and current-signature limits in `docs/architecture.md`.

### Required Interfaces and Contracts

- Document Python source/stub coverage, the index's `src/` naming convention (no inferred `src.` import alias), visibility heuristic, import-only references, unresolved/ambiguity semantics, no runtime call graph, one-hop module selection versus intersected context scopes, no automatic index maintenance, and checked-snapshot rather than live-source guarantees. Doc examples use relative source locations and never require a DeepSeek key.

### Requirement Evidence

- Original Request acceptance and exclusions; `.ai-factory/ARCHITECTURE.md` (boundaries); `v2-01-code-indexer` read-only/lazy compatibility. The `bug_fix` source artifact exercises the real fixture instead of only synthetic fixtures.

### Error Handling and Logging

- No new runtime logging. In tests, assert fixed public error codes and use existing offline fixtures; avoid recording environment secrets or absolute cwd in docs and fixtures.

### Tests

- Add the representative case in `tests/code_index/test_offline_e2e.py`. Run existing context and CLI workflow suites unchanged; no live network tests.

### Acceptance Criteria

- Traceable real-file range moves after explicit update; deterministic output before edits; existing prompts and offline worktree acceptance pass; docs agree with shipped behavior.

### Verification

- `uv run --locked pytest tests/code_index tests/context tests/repository_map tests/cli` and `uv run --locked pytest` — offline tests pass.
- `uv run --locked wl map --help` and `uv run --locked lean-weave map --help` — identical map command availability without an index or API key.

## Phase Risks and Mitigations

- Risk: framework adds output bytes or touches config on help. Mitigation: exact-output and lazy-help regressions, including the installed aliases.

## Phase Completion Checklist

- Both tasks meet acceptance criteria and verification passes.
- Update only the corresponding task checkboxes in `index.md`.
