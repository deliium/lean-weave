# Implementation Plan: Minimal Token-Efficient Context Builder

Plan ID: v1-04-context-builder  
Branch: none (exact plan identifier requested; existing branch unchanged)  
Created: 2026-11-18

## Original Request

Plan ID: v1-04-context-builder
Plan Title: Minimal Token-Efficient Context Builder

Dependencies:
- v1-01-architecture
- v1-02-project-foundation
- v1-03-deepseek-gateway

Goal:
Build a deterministic context retrieval system that supplies minimal relevant project information to LLM stages.

The Context Builder must not require LLM calls.

Requirements:
1. Scan repository structure while respecting .gitignore and configured exclusions.
2. Use Python AST to extract Python classes, functions, methods, and signatures.
3. Implement text search using ripgrep with a documented fallback.
4. Implement symbol lookup by name and file.
5. Extract simple Python import dependencies.
6. Read Git status and recent diffs.
7. Generate a compact repository map.
8. Rank files and symbols using:
   - exact symbol matches
   - textual relevance
   - dependency relationships
   - task-related paths
   - recent changes
9. Build ContextPack containing:
   - task description
   - relevant file paths
   - selected symbols
   - source ranges
   - applicable constraints
   - token estimates
10. Implement configurable context budgets.
11. Support progressive context expansion:
   - repository map
   - relevant symbols
   - specific source ranges
   - full files only when justified
12. Prevent repeated inclusion of identical code fragments.
13. Exclude secrets, generated artifacts, and irrelevant large files.
14. Provide CLI commands:
   - aif context search
   - aif context build
   - aif context inspect
15. Add deterministic tests using fixture repositories.

Token-efficiency requirements:
- Use no LLM calls during indexing or retrieval.
- Avoid sending entire files by default.
- Provide source provenance for every extracted snippet.
- Apply token budgets before building LLM requests.
- Prefer relevant source code over generated prose summaries.
- Never truncate code silently in a way that changes its meaning.

Constraints:
- Focus on Python projects in MVP 1.
- No embeddings or vector database.
- No mandatory Tree-sitter dependency.
- No LLM-generated descriptions of every file.
- Avoid building a complex persistent indexing service.
- Keep interfaces extensible for MVP 2.

Acceptance criteria:
- Relevant functions can be located without LLM calls.
- Context packs respect configured budgets.
- Context expansion is supported.
- Source references are accurate.
- Tests verify relevance, exclusions, and deterministic behavior.

Save the plan using the exact identifier:
v1-04-context-builder

## Settings

- Testing: yes
- Logging: verbose; honor `LEAN_WEAVE_LOG_LEVEL` / `LOG_LEVEL`, log safe metadata only
- Docs: yes; mandatory `/aif-docs` checkpoint
- Dependencies: [v1-01-architecture](v1-01-architecture.md), [v1-02-project-foundation](v1-02-project-foundation.md), and [v1-03-deepseek-gateway](v1-03-deepseek-gateway.md)

## Roadmap Linkage

Milestone: "ContextProvider"  
Rationale: This plan delivers deterministic stage-scoped retrieval, allowlists, provenance, and prompt budgets without depending on StateStore or the orchestrator.

## Requirements Reconciliation

The request sets feature scope. During planning the user confirmed existing executable names only: requirement 14 means `wl context` and `lean-weave context`, consistent with v1-02; no `aif` executable. Project rules and v1-01 stage isolation constrain what retrieval can admit. Ranking operates **inside** an eligible stage scope, never widens it. Preview commands are local, explicitly requested, and do not represent a workflow stage. The roadmap is a scope indicator, not a behavioral spec. All supported modes are read-only: no index, packs, transcripts, or usage records are persisted, and no LLM, orchestrator, or StateStore is called.

| Supported combination | Source and decision | Verification |
|---|---|---|
| Git worktree rooted at the project + `rg` available, or Git absent/unusable and/or `rg` unavailable | Request 1, 3, 6; v1-01 isolation. Nested `.gitignore` and hard exclusions apply in every mode. Git supplies optional path/change summaries only. Python literal search gives equivalent ordered results; invalid input is an error, not a fallback. | Fixture repository before Git init, after staged/unstaged changes, and with both search backends. |
| Local preview with map, symbols, ranges, or explicit full expansion | Request 2, 4, 7–13. Map contains compact structural metadata; symbols add signatures; ranges add complete selected units; full replaces narrower source only for an explicitly selected eligible path with a justification and enough budget. Inspect uses the same admission rules. | Same task at successive levels; explicit full success and denial; compare source hashes, omissions, and estimated tokens. |
| Plan stage: request plus approved project metadata | v1-01 token principles § Stage-scoped prompts. Accept `FeatureState.request` and optional bounded approved description/architecture digests; do not run repository discovery, search, or Git. | Assert no repository scan or subprocesses and no other-stage content in rendered messages. |
| Implement stage: active task plus its eligible task paths, one-hop imports, and matching tests when enabled | v1-01 token principles §§ Stage-scoped prompts, Deterministic file selection refine request 5, 8. Discover candidate paths without reading their bodies; read only approved task files, resolved imports and enabled matching tests. Add compact plan goal, known settings/task titles, and bounded prior outcome digests; no raw transcripts or other task bodies. | A recording reader proves unrelated higher-scoring source is never opened; test disabling tests and missing/ambiguous active task. |
| Retry stage: active task plus failure digest and bounded affected-file excerpts | v1-01 token principles and isolation. Require a failed verification result; build the digest from typed status, blocking flag and eligible affected paths. Read approved task sources if needed to resolve imports, but emit source only for eligible affected files. Verify rejects LLM-context building before discovery. | Failed state with mixed eligible/forbidden affected paths and a raw-log canary, missing/non-fail verification, stage mismatch, and unsupported verify. |
| Optional source cannot fit vs. required envelope cannot fit | Request 9–12; v1-01 budget defaults. Omit whole optional units with reason codes; raise a typed budget error if the required envelope cannot fit. v1-03 still separately estimates provider wire cost and enforces feature spend. | Exact boundary and one-over cases with Unicode, serialized overhead, tight limits, and repeated fragments. |
| Real project integration | Existing `src/lean_weave/app.py`, product configuration and CLI are the baseline, not just synthetic fixtures. | Offline end-to-end preview of `create_llm_provider` using `src/lean_weave/app.py`; compare signature and source range with the tracked file. |

## Commit Plan

- **Commit 1** (after tasks 1–3): `feat: add bounded Python repository discovery and indexing`
- **Commit 2** (after tasks 4–6): `feat: rank and build deterministic stage-scoped context packs`
- **Commit 3** (after tasks 7–9): `feat: expose offline context commands and verify context retrieval`

## Tasks

### Phase 1: Safe discovery and indexing

- [x] Task 1: Define context models, limits, and errors.
  - Add `src/lean_weave/context/models.py` for `ContextQuery`, expansion level, `SymbolRef`, `SourceRange`, `ContextPack`, and safe omission reason codes. Use normalized project-relative POSIX paths, 1-based inclusive lines, and a content digest over the canonical decoded snapshot. Keep trusted constraints separate from repository data. Make the pack carry the task, relevant paths, selected symbol metadata, source and provenance, constraints, and final estimated prompt tokens; keep diagnostic-only omissions outside the rendered prompt.
  - Add strict `context` settings in `src/lean_weave/config/models.py` and wire defaults through `config/loader.py`, init-generated YAML, and safe config inspection. Default to 6,000 stage prompt tokens, 12,000 inline chars per file, 40,000 total code chars, 2,000 chars per digest, 8,000 combined retry excerpt chars, and no history. Add bounded scan/search knobs with defaults of 128 KiB per candidate, 8 MiB total candidate content, 20,000 visited entries, 100 search hits, and 10 recent commits. Validate finite positive caps and normalized relative exclusion patterns; per-call overrides can only tighten limits. Use safe typed errors in `src/lean_weave/errors.py` for invalid/forbidden input, scan failure, and required-budget exhaustion; retain current error and config exit conventions.
  - Name the settings `stage_prompt_tokens_max`, `per_file_chars_max`, `total_code_chars_max`, `digest_chars_max`, `retry_excerpt_chars_max`, `candidate_bytes_max`, `scan_bytes_max`, `visited_entries_max`, `search_hits_max`, `recent_commits_max`, and `exclusions`. No configurable history field. `config/loader.py` already generates init YAML from `AppConfig`; extend the model rather than creating a parallel template. In `cli/commands/config.py`, show numeric context settings and `exclusion_count` instead of raw exclusion strings, in both YAML and JSON; leave the on-disk data shape unchanged.
  - **Evidence:** Reconciliation budget row; tests under `tests/config/` for defaults, old YAML, new round trip, unknown fields and invalid values; check both `config show` formats for exclusion canaries independently of logs. **Logging:** DEBUG effective numeric limits and validation outcome codes only; pure models need no logging.

- [x] Task 2: Enforce a single repository eligibility and read policy (depends on 1).
  - Create `src/lean_weave/context/repository.py` and `policy.py`. Walk the canonical project root in sorted order without following symlinks, including directory symlinks. Apply nested `.gitignore` rules in directory order with negation and excluded-parent semantics, in and out of Git. Use the same rules for tracked files and untracked files. Use a vetted Git-wildmatch implementation (`pathspec`, narrowly added to `pyproject.toml`, `uv.lock`, and `tests/test_dependency_policy.py`) rather than a custom partial matcher. Configured exclusions are additive; neither negation nor explicit selection overrides hard bans. Never traverse nested repositories.
  - Hard-deny VCS metadata, `.lean-weave` and any configured state directory, skill trees and AI Factory working artifacts, credentials/keys and env files, dependency/build/cache/coverage directories, and known generated or lock files. Accept supported text types for map/search, with Python source for AST; reject binary, undecodable, oversized, and non-regular files. Validate each path component and containment again at read time, use bounded no-follow reads, and skip changed/unsafe snapshots instead of reopening unchecked paths. Quarantine files with supported high-confidence credential patterns before path, signature, or source exposure; reject detected secrets in supplied task text/constraints without echoing them. Document this as a bounded filter, not a universal secret detector.
  - Separate path inventory from safe content admission. Bound entries including excluded entries, ignore-file reads, and all attempted content reads, including later rejects. Reuse a single canonical snapshot per admitted file and operation across search, AST, and rendering. Keep the root at canonical cwd rather than the config file's parent or Git top-level. Refuse a scan with a safe configuration error if the configured state directory resolves to the root. Surface scan-limit or unreadable-file omissions by safe reason; default-denied files must not be opened for indexing or passed to search subprocesses. Use non-following checks for dangling as well as live symlinks.
  - **Evidence:** Eligibility fixtures for nested ignores and re-inclusion, Git parity, generated/secret files, outside-root and symlink paths, binary/large files, modified snapshots, and scan limits. **Logging:** DEBUG counts by decision code; WARN on incomplete scans without exposing excluded paths or content.

- [x] Task 3: Build the Python symbol/import index and compact repository map (depends on 2).
  - Implement `src/lean_weave/context/python_index.py` and `repository_map.py`. Parse safe `.py`/`.pyi` snapshots without importing modules. Index classes, sync/async functions, and methods by simple and qualified name and file, preserving duplicates with distinct ranges. Include decorators in definition spans, preserve parameter/return shape, and use AST line coordinates plus original decoded lines for excerpts. Cover positional-only and keyword-only parameters, annotations, nested/async definitions, and Unicode. Surface syntax or encoding issues without losing unrelated files.
  - Extract simple static `import` and `from` edges, including relative imports for unambiguous root/`src/` packages. Keep only resolvable in-repo targets, deduplicate, sort, and cap expansion at one hop. Never import modules or follow dynamic/external imports. Produce a compact path/module/symbol-count map with deterministic limits and explicit omissions; do not generate per-file prose.
  - **Evidence:** Fixture tests for symbol lookup by name/file, exact decorated spans, ambiguous/relative imports, package cycles, and broken source. **Logging:** DEBUG parse and resolution counts; no signatures, source, or raw parser exceptions in logs.
<!-- Commit checkpoint: tasks 1–3 -->

### Phase 2: Retrieval, ranking, and packing

- [x] Task 4: Add equivalent literal search backends and optional Git recency (depends on 2).
  - Add `src/lean_weave/context/search.py` and `git_metadata.py`. Search a nonempty, single-line, NUL-free literal case-sensitively; return one hit per matching line, ordered by path and line, subject to the configured cap. Use one LF-based line iterator over the canonical snapshots for both backends and provenance, without inventing a final empty line. For `rg --no-config --fixed-strings --json`, encode each logical line as UTF-8 with one LF terminator and keep a parallel `(path, original_line)` table; add no separators between files. Use `--` before the query. Never let `rg` open repository paths. Treat exit 1 as no matches; on missing executable, timeout, output overflow, malformed indices, or backend error, discard partial results and use Python substring matching over the same lines.
  - In a Git worktree rooted at the project, read NUL-safe porcelain status plus staged/unstaged and recent committed diff path/status summaries, including linked worktrees and unborn HEAD. Limit history to the configured number of commits; disable lazy fetch, optional locks, filesystem monitor hooks, and external diff/textconv. Set an explicit root and `shell=False`; do not read or emit diff bodies, commit messages, or historical content. Filter results through the current eligible path set; only use an eligible rename destination. Git absent or unusable means neutral recency with a reason code. Use a shared bounded subprocess helper with fixed argv structure, timeout, output limit, and safe failure mapping.
  - Treat Git configuration as untrusted: use an isolated child environment without inherited Git overrides or global/system configuration; if effective repository configuration can invoke external helpers (including clean/process filters), or its safety cannot be established, skip the optional signal. Run Git only for preview, not workflow-stage builds.
  - **Evidence:** Compare rg/Python output across multiple files with CRLF, Unicode, no final newline and leading `-` queries, plus empty/no-match, timeout and overflow cases. Cover staged/unstaged/untracked/recent/rename cases and verify no Git mutation or external helper execution on the actual subprocess path. **Logging:** DEBUG backend and hit/change counts; WARN on degraded mode with generic reason, never argv query or raw subprocess output.

- [x] Task 5: Rank eligible files and symbols deterministically (depends on 3–4).
  - Add `src/lean_weave/context/ranking.py`. Tokenize task terms deterministically and accept explicit symbol/path hints. Rank qualifying candidates lexicographically by exact qualified/simple symbol match, explicit task path, capped distinct query-term matches, one-hop import relationship, inferred path relevance, then bounded recency. A recent change alone cannot qualify an otherwise unrelated candidate. Break ties with normalized path, qualified name, and source coordinates; do not rely on walk order, timestamps, or runtime hash order. Rank both files and symbols with the same documented precedence and lift matching symbol signals to the parent file. Match tests by exact module naming convention only and only when enabled for a workflow stage.
  - **Evidence:** Fixtures with exact-name collisions, misleading keyword repetition, one-hop dependencies, explicit/inferred paths, tied scores, recent unrelated changes, and alternate traversal order. **Logging:** DEBUG aggregate counts per signal; no raw queries or task descriptions.

- [x] Task 6: Assemble packs, progressive expansion, and the stage adapter (depends on 1–5).
  - Implement `src/lean_weave/context/builder.py`, `render.py`, and `provider.py`. Normalize, filter, rank, and admit whole candidates in stable order. Default build to symbols; use map only, map plus selected signatures, or targeted ranges according to requested expansion. Automatic ranges are complete AST def/class spans; a manually selected non-AST line interval is labeled explicitly as a partial excerpt. Full-file mode requires a specific eligible path and a nonempty justification, including when an adapter caller authorizes it; otherwise return a typed validation error. For an over-budget candidate, keep the admitted lower-detail representation and report the omission; never cut a code unit to hit a limit.
  - Canonicalize newline handling but preserve indentation and code text. Cite every excerpt with path, original line range, and snapshot digest. Retain only one copy of identical code and associate alternate source ranges as provenance. Avoid overlapping same-file bytes: prefer the more specific already-admitted unit or atomically replace contained pieces with a complete larger unit if it fits. Do not print a signature twice when its body already contains it. Report omitted or superseded units without hiding provenance or presenting partial units as complete.
  - Render a single canonical system/user message pair; delimit untrusted repository data from trusted constraints and never treat it as instructions. Reuse `metrics/budget.py::estimate_tokens` on the complete serialized message list, not summed independent estimates; check the required envelope first, then each proposed addition with all formatting and citation overhead. Enforce per-file, total-code, and retry caps as well. No journal writes or provider imports/calls; local prompt estimates are not measured usage or gateway wire budgets.
  - Preserve `contracts/context_provider.py::ContextProvider.build(stage, feature_state)` and `ContextBundle` compatibility; add a typed pack field and populate legacy views from the same result without placing them in messages a second time. Require stage/state agreement and use only explicit typed state fields per reconciliation; never consult opaque metadata or interrupt payloads. Resolve a unique active task; validate path hints before scope expansion. Use bounded structured project digests from a narrowly approved metadata source, never general skill-tree access; read no raw artifact or runtime transcript into prompts. Retry uses only safe structured failure fields and eligible affected excerpts, not arbitrary verification dicts or raw logs. Do not mutate state or choose transitions. Add lazy construction in `src/lean_weave/app.py`, honoring injected providers, with no scan in `build_app_context` or CLI help.
  - Expose one typed message accessor over the canonical prompt pair. `file_excerpts`, `digests`, and the pack are inspection views, never appended by the caller; use the same render-and-admit path for inspect selections. Plan and verify must not open source. For implement/retry, inventory paths first and prefilter content reads to task paths, import targets discovered from admitted task files, and matched tests only when `settings["testing"] is True`. On retry, admit only affected source from that scope; ignore `blockers`, `gate_summaries`, and `suggested_next` in the digest rather than trusting arbitrary diagnostics.
  - **Evidence:** Render-to-estimator equality including legacy views, Unicode and exact cap checks, overlap/dedup and source provenance tests, all stage rows in reconciliation, and no-work/no-network tests. **Logging:** DEBUG counts, budgets and omission codes; safe operational errors for forbidden context, invalid state, or required envelope overflow. Never log prompts, task text, file contents or secret-bearing paths.
<!-- Commit checkpoint: tasks 4–6 -->

### Phase 3: CLI and acceptance

- [x] Task 7: Expose offline preview through the shared Typer CLI (depends on 6).
  - Register `context` in `src/lean_weave/cli/app.py`; add `cli/commands/context.py` using `command_context` and the same retrieval service as the stage adapter. `search QUERY [--path PATH] [--limit N]` returns bounded path/line hits and recognizes exact symbol names via explicit `--symbol`; `build TASK [--path PATH] [--level map|symbols|ranges|full] [--reason TEXT]` returns a pack; `inspect PATH [--symbol NAME | --lines START:END]` returns safe metadata by default or a provenance-bearing selected range. `build --level full` requires exactly one path and a reason. Validate path, level-specific flags, and search limits at the boundary. Provide stable `--json` output for automation and a concise human-readable default. No state write, auth key requirement, or usage journal; preserve exit 0 for zero search hits, 1 for operational errors, 2 for invalid usage, 3 for configuration errors. Sanitize diagnostics and terminal control characters without editing the JSON source payload.
  - Define exit 2 for malformed selectors, including invalid line bounds or conflicting flags; exit 1 with the same generic diagnostic for a valid explicit path that is unavailable or forbidden; and success with stable omission codes when optional material cannot fit, including an oversized full-file candidate that falls back to lower detail. An oversized required envelope is an operational error, not an empty success. Keep success stdout to one response; escape terminal controls in human output and use JSON serialization for control escaping without rewriting admitted source. Reject configured secret values and supported credential patterns before serialization in every outward field, including paths and task text; never rewrite an admitted snippet to mask a secret and then report its original digest as if it were unchanged.
  - **Evidence:** `tests/cli/test_context.py` exercises `CliRunner` and the exported `main(argv)` boundary, both entry points, help, generated product YAML, invalid flags, exclusions, provenance, partial results, and budget failures. Check human stdout, decoded JSON, stderr, and application logs separately. **Logging:** INFO operation name and result counts; DEBUG configured level and estimate; safe typed errors on stderr without query/source echo.

- [x] Task 8: Verify end-to-end determinism and token efficiency (depends on 7).
  - Add reusable fixture repos under `tests/context/` and end-to-end tests through the real scan/search/rank/pack/CLI path. Use local Git with controlled fixture timestamps, no remotes, and a no-Git variant. Exercise an actual `rg` install where available and simulate its absence everywhere; keep both Python-path and CLI tests independent of external tools. Assert normalized packs byte-for-byte on identical inputs, with irrelevant/reordered candidates, bounded expansion, secret canaries in both output and captured logs, exact source slices, and provider/network sentinels. Run an offline smoke case against the real `src/lean_weave/app.py` and verify the `create_llm_provider` symbol and line coordinates. Run `uv run --locked pytest` and `uv run --locked python -m compileall -q src/lean_weave`.
  - **Evidence:** Covers every reconciliation row and all acceptance criteria in the original request. Run the tracked `src/lean_weave/app.py` case through the exported CLI using generated product configuration; cross-check JSON provenance against the canonical file snapshot, and test production fallback paths rather than only injected doubles. Treat subprocesses separately from the pytest socket guard. **Logging:** Use captured logs to assert safe degradation and omission codes; do not introduce test-only production logs.

- [x] Task 9: Complete the required documentation checkpoint (depends on 8).
  - Use `/aif-docs` to update `docs/cli.md`, `docs/getting-started.md`, and `docs/architecture.md` with commands, examples, configuration limits, supported literal/AST semantics, Git and rg fallback, stage boundaries, exclusions and their limitations, expansion and provenance, and estimate vs. feature spend. Document that workflows are not wired by this milestone and that context preview runs locally without API credentials. Cross-check examples against tested CLI shapes.
  - **Evidence:** Run the documented offline search/build/inspect examples. **Logging:** Document existing controls and what is never logged; no new runtime logging.
<!-- Commit checkpoint: tasks 7–9 -->
