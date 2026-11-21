# Phase 3: Compact views and opt-in context preview

Plan: [index.md](index.md)  
Tasks: 4–5  
Depends on: Phase 2 / Tasks 2–3

## Objective

Expose predictable, budgeted map queries and a map-only ContextPack preview while preserving default retrieval, prompts and workflow integration.

## Current-Code Evidence

| Path | Symbols | Why it matters |
|---|---|---|
| `src/lean_weave/config/models.py` | `ContextConfig`, `ConfigModel` | Add strict limits with defaults without requiring changes to existing config files. |
| `src/lean_weave/context/builder.py` | `ContextBuilder.build`, `BuildResult`, `_messages` | Existing map admission precedes excerpts; keep that branch unchanged. |
| `src/lean_weave/context/{models,render}.py` | `ContextQuery`, `ContextPack.repository_map`, `render_messages`, `UNTRUSTED_START/END` | Reuse the pack's `list[str]` map field and exact full-envelope estimator. |
| `src/lean_weave/cli/commands/context.py` | `build_command`, `_path`, `_emit` | Existing build preview can opt in explicitly; JSON shape remains unchanged. |
| `src/lean_weave/app.py` | `create_code_index`, `create_context_provider` | Reuse index construction; leave provider wiring untouched. |
| `src/lean_weave/code_index/index.py`, `src/lean_weave/context/repository.py` | `CodeIndex.root` / `_policy_key`; `Repository.root`, `protected_paths` | Match the checkout and admission policy before accepting an injected preview. |
| `src/lean_weave/context/__init__.py` | eager export of `ContextBuilder` | A runtime builder import of the map service would cycle through CodeIndex and this package. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `src/lean_weave/repository_map/{service,render,__init__}.py`, `src/lean_weave/errors.py` | create, modify | Reusable service, ranked views, renderer and safe selector errors. |
| `src/lean_weave/config/models.py`, `src/lean_weave/app.py` | modify | Optional limits with defaults; lazy service factory. |
| `src/lean_weave/context/{builder,render}.py`, `src/lean_weave/cli/commands/context.py` | modify | Explicit offline indexed-map preview only. |
| `tests/repository_map/{test_views,test_context_preview}.py`, `tests/cli/test_context.py` | create, extend | Ranking, budgets, lazy integration and exact default behavior. |

## Task 4: Rank and render compact map views

### Intent

Provide useful deterministic output without source dumps, graph-algorithm dependencies, or duplicate definitions.

### Implementation Steps

1. In `service.py` add `RepositoryMap(index: CodeIndex, *, limits: MapLimits)` with `show()`, `module(selector)`, `dependencies(selector)` and `references(selector)`, returning a frozen `MapResult(revision, rows: tuple[str, ...], row_paths: tuple[str, ...], text, estimated_tokens, truncated)`. Add a private context projection used by Task 5's bound `for_context` entry point. Each row path is the primary source of its aligned row. Each call validates selectors first, reads one checked generation in a shared private `_load` method, then refreshes the private memo once via Task 3. `None` scope is explicitly global; an empty set means no candidates. An instance is intended for sequential reuse, not concurrent mutation.
2. Require 1–512 UTF-8 bytes of dot-separated Python identifiers excluding keywords and rejected credential-like data. Module selectors are exact indexed names with unique file identity. Symbol selectors are exact fully qualified names if dotted, exact simple names otherwise; repeated occurrences of one `(file_id, local_name, kind)` are one selector group, not unrelated matches. Multiple groups are ambiguous. `references` reports all same-group definition locations plus only statically unambiguous import sites from Task 2; for repeated occurrences these may produce no import sites. No lexical guesses about method callers. Use `RepositoryMapError` (`invalid_selector`, `not_found`, `ambiguous_selector`) with safe fixed messages.
3. Define public candidates as names whose own and ancestor components do not start `_`, with only module-level classes/functions and class methods (including async, and methods of nested classes reached through class-only ancestors); no closures. No `__all__` inference. Global ordering: descending distinct incoming local importers, then descending distinct outgoing targets, then module name/path. Exclude global candidates with neither public symbols nor a resolved non-self edge, and those in a `tests` directory or with stems `test_*` / `*_test`. Module view ranks the selected module first, then its direct resolved outgoing/incoming neighbors only; selected file metadata is shown even without definitions. Rank symbols within each file by descending distinct importer count of explicit references, then class/function/method, qualified name, start line, end line. Submit each candidate module header followed by its best symbol (if any) in module rank order before submitting remaining symbols by module/symbol rank; each row is independent. Filter to the query's candidate set before ranking; explicit context path scopes retain isolated/test files. Dependencies view shows direct outgoing import occurrences (including status/reason for partial, ambiguous and missing) and incoming resolved occurrences with separate direction labels. Do not recursively traverse cycles.
4. Evaluate the baseline using a deterministic hub/leaf/sparse fixture: compare against lexical-only order by counting whether the expected hub and its explicitly imported public symbol appear within six rows. Give the hub a name that sorts after three leaf modules to make the comparison meaningful. Separately confirm the public `calc.add` remains prominent on `bug_fix` in Task 7. Encode baseline winners in tests and document why PageRank is deferred (raw import evidence lacks runtime semantics; extra dependency and iteration buy nothing proven for these fixtures). No experimental graph algorithm ships.
5. In `render.py` render independently usable one-line units: module `module :: path`, definition `path:start-end qualified :: signature`, dependencies `in|out path:start-end spelling [as alias] -> target/status(reason)`, and references `def|import path:start-end ...`. Keep digest/revision on typed refs, not repeated in compact text. Escape terminal/control/format characters, line separators, and `<`/`>` in repository fields before budget measurement (leave structural syntax intact). Reuse `contains_secret` checks on textual metadata and drop any unsafe row without disclosing it; no source bodies or raw decorators. Definition dedup key is `(file_id, local_name, kind, normalized signature)` with earliest range as representative for map views; distinct overload signatures remain; references preserve each distinct evidence range. Offer a short definition form `path:start-end qualified [kind]` in place of a signature over 160 escaped characters or one that does not fit the remaining budget; never cut syntax or provenance. Mark budget-driven shortening as truncation. Deduplicate after selecting forms based on semantic identity, retaining distinct source ranges for references. For dependency and reference rows, order within each group by path, span, and statement/name ordinal. Interleave incoming and outgoing rows by ordinal within those sorted lists, starting with incoming when both exist, then emit the remaining direction after the other runs out. For `references`, present definitions first, sorted by location, then import sites.
6. `MapLimits` has `tokens_max=1024` (1–8192) and `rows_max=80` (1–500). In `ContextConfig` mirror them as strict `map_tokens_max` / `map_rows_max`; apply to all views. Existing YAML remains valid; generated config and `wl config show` add those two numeric defaults via existing serialization. Charge actual UTF-8 bytes of emitted text (including each final LF; one byte per estimated token; local conservative size metric, not provider-token guarantee). Greedily admit complete rows in the specified order, trying the short definition form before skipping. `truncated` means a budget drop or shortening, not unresolved data. Append `...` plus LF only if it fits both caps without removing evidence, including when all evidence was dropped. If there are no rows and the full marker cannot fit but truncation occurred, emit a single `.` with no newline; this fits the minimum valid budget. Keep markers out of `rows`; `text` includes them and `estimated_tokens` counts them. A genuinely empty view emits no bytes. No extra headings or footers outside budgets. Do not reserialize accumulated output per candidate.

### Required Interfaces and Contracts

- `for_context` uses module/symbol rows only; no edges, aliases or resolution diagnostics rendered. When scope is present, rank and filter using induced admitted paths only; no neighbor expansion. A supplied nonempty scope with missing indexed paths is rejected with safe `not_found`, not silently treated as global. All views return stable bytes for identical checked facts, limits and selector.
- No map outputs affect source retrieval ranking or write/tool authority. No SQLite schema change, network access or parsing. Tests/diagnostics stay offline.

### Requirement Evidence

- Original Request 1, 5–14 and token efficiency/acceptance; reconciliation query, visibility, budget and statically provable reference rules. Aider review in index explains baseline choice.

### Error Handling and Logging

- Propagate checked-generation errors. Invalid/ambiguous selectors never emit partial matches. `DEBUG [RepositoryMap.view]` view, row count, estimated tokens and truncated only; never log source fields or selectors.

### Tests

- `tests/repository_map/test_views.py`: exact goldens for repeated runs and reversed construction order; lexical baseline comparison, test-only exclusions, selected private module metadata, one-hop exclusion, overloaded/repeated definitions, unique/ambiguous selectors and absence of guessed references; control/marker escaping and secret suppression; empty, tiny, exact and multibyte budgets in all views. Assert a budget-limited definition retains its range in short form, the one-byte sentinel is distinguishable from an empty result, and a two-row dependency budget admits both directions despite many outgoing rows. Generate a sparse 2,000-module fixture or typed checked-generation double within existing source limits; assert bounded output, iteration proportional to catalog/edge counts plus sorting, and no AST or recursive traversal.

### Acceptance Criteria

- Public API provides globally ranked and scoped results, both import directions, precise evidence and compact deterministic output within configured limits.

### Verification

- `uv run --locked pytest tests/repository_map/test_views.py` — ranking, no duplicates and budget checks pass.

## Task 5: Integrate explicit indexed ContextPack preview

### Intent

Let existing offline context callers consume repository map rows without changing any workflow provider calls or default ContextPack output.

### Implementation Steps

1. Add `app.py::create_repository_map(context, *, workspace_root=None, cache_base=None)` wrapping `create_code_index` and constructing `MapLimits` from `ContextConfig`; construction does no scan or database open. Add `RepositoryMap.for_context(*, allowed_paths: frozenset[str] | None, repository: Repository) -> MapResult` with the required builder repository argument at this boundary. Inside the service before `_load`, compare `context.policy.safe_root(index.root)` with `repository.root`, then require equal state directory, protected paths, exclusions, and candidate, scan-byte and visited-entry limits. A root or policy mismatch is `ForbiddenContext` with a fixed message, before scanning or opening the index. `ContextBuilder` prompt limits remain independent of source-admission identity.
2. Add keyword-only `indexed_map: RepositoryMap | None = None` to `ContextBuilder.build` using postponed annotations and a module-level `TYPE_CHECKING` guard for the map type (avoid the import cycle; no local imports). When omitted, keep the existing path unchanged. When supplied, require `query.stage is None`, `query.level == ExpansionLevel.MAP`, no line range, symbol selector or exact-symbol mode; reject invalid combinations before I/O. Enter a dedicated preview method **before** `build_python_index`, Git metadata lookup or ranking. Pass the builder repository to `for_context`; intersect `query.scope_paths` and `query.paths` if both are nonempty, otherwise use the one supplied, and use `None` only when neither is supplied. A disjoint intersection is an explicit empty set, not global. Keep the adapter preview-only; do not inject it into `DeterministicContextProvider` or change the provider protocol.
3. Render the existing required envelope with no map, preserving trusted constraints and supplied metadata digests; reject if it already exceeds `stage_prompt_tokens_max`. Use `for_context`, then admit complete rows in order only if `estimate_tokens(render_messages(...))` of the full candidate still fits. Preview-only rendering wraps the map section in `UNTRUSTED_START/END`; leave default rendering byte-identical. Add a `MAP_LIMIT` omission with safe fixed detail for truncation by either budget. Omit the CLI's truncation marker from pack rows. Return the existing `BuildResult` and `ContextPack` shapes, empty selected symbols/excerpts/file digests, and `relevant_paths` from emitted row provenance only (keep an aligned typed path per row on `MapResult` for this projection). Nothing in the path invokes an LLM.
4. Add `--indexed-map` to `wl context build`, valid only with `--level map`; wire the lazy factory, keep JSON shape, and show the selected map lines in the human output only on opt-in. Give the opt-in builder `Repository` the same root, config, state dir and protected config paths as its `CodeIndex`: reuse `app.py::_protected_index_paths` via an app-level factory for constructing this pair; keep legacy `cli/commands/context.py::_repository` unchanged. Validate explicit paths using existing normalization/credential guard and checked index membership; skip the legacy preflight `is_eligible` in the opt-in branch. Reject incompatible flags before reading the index. No opt-in for `context inspect`, no YAML toggle for workflow or its stage calls.

### Required Interfaces and Contracts

- Pack remains `repository_map: list[str]`; default CLI JSON, digest views, messages, and stage behavior stay byte-for-byte unchanged. Scope cannot be expanded by the graph. Indexed preview reads the whole checked generation for freshness, but emits only allowed paths. It fails closed instead of falling back to legacy parsing on an explicit cache request.

### Requirement Evidence

- Original Request architecture and 15–17; `.ai-factory/RULES.md` stage isolation; V2-01 Tasks 6–7 and reconciliation preview/default cases. Verify opt-in visibility versus legacy byte stability and zero plan/verify I/O changes.

### Error Handling and Logging

- `DEBUG [Context.indexed_map]` outcome, row count, budget drop count and envelope estimate only; surface safe map/index codes with no repository or selector text on refusal. Legacy diagnostics untouched.

### Tests

- `tests/repository_map/test_context_preview.py`: opt-in global and explicit scopes, overlap and disjoint intersection, unknown/empty corpus, staged and incompatible mode rejection, no source parser or Git path invocation, tiny prompt budget and exact pair estimate, no duplicate map/signature surfaces, trusted system and marker-escape checks. Inject a foreign checkout with the same relative filename and a same-checkout index with looser exclusions/limits; both must fail before index I/O. Extend `tests/cli/test_context.py` for opt-in JSON/human, protected paths with `--config`, rejection and stale index; run existing provider/CLI/default pack tests unchanged.

### Acceptance Criteria

- Existing provider protocol, default messages, scope, excerpts and approvals do not change; explicit preview exports only budgeted admitted rows and remains offline.

### Verification

- `uv run --locked pytest tests/repository_map/test_context_preview.py tests/context tests/cli/test_context.py` — isolated preview and unchanged default behavior pass.

## Phase Risks and Mitigations

- Risk: rich rows crowd out source excerpts or widen the stage. Mitigation: dedicated map-only preview branch, no shared workflow strategy switch, existing scope and provider kept intact.
- Risk: output appears safe under a standalone cap but exceeds the envelope. Mitigation: check both independently on the exact final representation.

## Phase Completion Checklist

- Both tasks meet acceptance criteria and verification passes.
- Update only Tasks 4–5 in `index.md`.
