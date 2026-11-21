# Phase 2: Checked graph evidence and hybrid ranking

Plan: [index.md](index.md)  
Tasks: 3–4  
Depends on: Phase 1 / Tasks 1–2

## Objective

Rank task-related paths and concrete definitions in one pass over an admitted source snapshot, with explainable scores and useful cold-cache behavior.

## Current-Code Evidence

| Path | Symbols | Why it matters |
|---|---|---|
| `src/lean_weave/code_index/index.py` | `_checked_generation`, `read_generation`, `fact_loader` | Generic checked reads rescan Python; scoped facts alone do not prove graph freshness. |
| `src/lean_weave/repository_map/service.py`, `src/lean_weave/repository_map/graph.py` | `_load`, `_rank_modules`, `refresh_graph`, `_edge_worthy` | Consume typed graph, not budgeted map rows. Adjacency can contain known base modules from unresolved members; filter actual relations. |
| `src/lean_weave/context/git_metadata.py`, `src/lean_weave/verify/process.py` | `_environment`, `_UNSAFE_CONFIG`, `run_bounded` | Reuse sanitized local Git flags and bounded capture. V1 history is not diff relevance. |
| `src/lean_weave/context/python_index.py`, `src/lean_weave/context/ranking.py` | `extract_python_facts`, `module_name_for_path`, `matching_tests` | Use local facts in cold mode and existing test filename conventions. |
| `src/lean_weave/metrics/budget.py` | `estimate_tokens` | Estimate the cost of the referenced range using the existing conservative UTF-8 estimator. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `src/lean_weave/code_index/index.py`, `src/lean_weave/repository_map/service.py` | modify | Reuse an already admitted complete scan for read-only checked graph evidence |
| `src/lean_weave/context/git_metadata.py` | modify | Preview-only bounded staged/unstaged path evidence |
| `src/lean_weave/context/retriever.py`, `src/lean_weave/context/retrieval_ranking.py` | create | Request orchestration, normalized ranking and provenance selection |
| `tests/code_index/test_read_generation.py`, `tests/repository_map/test_retrieval_graph.py`, `tests/context/test_retrieval_git.py`, `tests/context/test_retriever.py`, `tests/context/test_retrieval_ranking.py` | modify/create | Strict snapshot checks and functional ranking |

## Task 3: Assemble checked graph and Git signals

### Intent

Use optional read-only inputs without rescanning the corpus or trusting stale or unauthorised metadata.

### Implementation Steps

1. Give the strict scan result a read-only `matches_retrieval_policy(repository)` check using the effective policy frozen by Task 1 and the live session's anchored root device/inode. Add `CodeIndex.read_generation_for_retrieval_scan(scan)`. Before opening the index lease, reject an incomplete scan with `CodeIndexError("incomplete")`, a detached/closed session or checkout/policy mismatch with `"invalid_request"`, and failed witness revalidation with `"storage_unavailable"`. Inspect under the existing `_read_lease`; factor the common healthy-store and header/revision checks out of `_checked_generation`. Compute `source_revision` from the verified frozen policy and admitted `is_python` snapshots only (ensure retrieval includes those extensions); check schema/health, `_location()` checkout namespace, policy/extractor/revision, plus record path/digest/line-count equality for the Python projection. Map revision/record drift to `"stale"` and an incompatible extractor/policy header to `"incompatible"`. Revalidate before returning the generation under the lease. Leave `read_generation()` behavior unchanged. Do not call `lookup`, `status`, `fact_loader`, or `scan_python_sources` from this new path.
2. In `RepositoryMap`, add `graph_for_retrieval_scan(scan)` using the new checked read, then `refresh_graph(self._graph, generation)`. Share the existing memo-update logic without changing query semantics. Return a defensive projection of the graph with copied adjacency/reference mappings (tuples and immutable edges/nodes can be shared). In retrieval, index `nodes`, `symbols` and `relations` once by ID/path. Include only `status == "resolved"` relations with a distinct target and both endpoint digests matching admitted snapshots (import provenance must match the importer digest too). Precompute incoming/outgoing unique non-self neighbor counts for importance. No runtime-use inference and no parsing of rendered map text.
3. Add `read_retrieval_diff(root, admitted_paths, *, run_capture) -> frozenset[str] | None` to `context/git_metadata.py`. Use safe existing env and disable hooks, fsmonitor, preload/untracked cache, external diffs, textconv, and rename detection, with fixed argv, a 5 s deadline and 1 MiB bounded capture per command. The app injects `verify.process.run_bounded` to avoid a context-to-verification import cycle. Accept initially only a real root `.git` directory (no parent lookup, gitfiles or symlinks). Verify `rev-parse --show-toplevel` equals the canonical root; use the existing unsafe-config predicate, factored to accept bounded command execution without altering V1. Read `git diff --name-status -z --no-renames --no-ext-diff --no-textconv --` and the same command with `--cached` for staged. Parse strict UTF-8 NUL pairs for recognized statuses; intersect `A/M/T` with admitted paths and ignore deletions, duplicates and non-admitted paths. One failed or malformed probe makes the signal `None`, not clean. No history, patch bodies, untracked matches or caller-supplied refs/arguments. Disabled or unavailable Git only removes that signal.

### Required Interfaces and Contracts

- Both optional signals consume the same request scan/admission policy. A stable partial scan skips both graph and Git; do not report either optional input as failed when it was not attempted. Revalidate the scan again immediately before returning retrieval results; on a failed witness discard the entire projection, not just its graph rows.
- Matching graph status is all-or-nothing for indexed ranking. Admitted snapshot content, not the index, establishes whether any source coordinates may be returned. Non-Python edits do not on their own change the Python revision.

### Requirement Evidence

- Original Request 2, 11–14; reconciliation **Checked index**, **Fallback**, **Git evidence**. V2-01 checked lifecycle and V2-02 typed graph provenance remain intact.

### Error Handling and Logging

- Map expected `CodeIndexError.code` to `index_missing|index_stale|index_incomplete|index_unavailable`. Incomplete source scans bypass indexed graph access. Git failures map to `git_unavailable` only when requested by the scoring path. DEBUG safe input status and aggregate counts; no raw diff records, graph signatures, stderr, paths or query text in new logs. Do not broadly catch unexpected exceptions.

### Tests

- `test_read_generation.py` and `test_retrieval_graph.py`: one shared strict scan and one healthy store read; reject detached/closed, foreign, changed-policy, incomplete, stale-digest and changed-witness cases with the specified codes; tripwire lease entry on precondition failures. Non-Python-only changes do not invalidate Python graph; no writes, maintenance, duplicate Python scan or AST reparse when current. Mutating returned mappings must not affect subsequent map views. Exercise resolved, unresolved-member, ambiguous, self and circular imports.
- `test_retrieval_git.py`: clean vs unavailable, staged and unstaged in a temporary repo, deleted/excluded/untracked changes, malformed or capped capture, unsafe config, gitfile and nested/no Git. Assert no external helpers or caller-controlled Git args.

### Acceptance Criteria

- Current graph and diff evidence can be attached to a single admitted source view without expanding it; missing/unsafe inputs are explicit, recoverable degradation.

### Verification

`uv run --locked pytest -q tests/code_index/test_read_generation.py tests/repository_map tests/context/test_retrieval_git.py tests/context/test_git_metadata.py` — all pass.

## Task 4: Rank files and symbols with provenance

### Intent

Produce one deterministic shortlist that favors specific evidence while keeping useful dependencies and avoiding redundant source ranges.

### Implementation Steps

1. Implement `ContextRetriever(repository, *, load_graph=None, read_diff=None).retrieve(request)`. The injected graph callable takes the strict scan; the diff callable takes root and admitted paths. Use type-only imports for checked graph types and no runtime dependency from `context` back into `code_index` or `repository_map`. Accept a typed request only. Open one strict text scan; use canonical snapshots once for query search, file/module metadata and cost. On current graph, reuse checked Python definitions (including private definitions and occurrence IDs); for missing/stale/incomplete/unavailable graph, parse each admitted Python snapshot once with `extract_python_facts`. Live definition ID is `live:` + SHA-256 of compact JSON `[path, local_name, kind, occurrence]` with UTF-8 canonical encoding; indexed definitions keep V2 symbol IDs. No symbol rows for syntax/parse-limit files or non-Python text; allow lexical and name file evidence. No stale rows or graph signals in fallback. An unavailable scan yields empty hits with `source_unavailable`; a stable partial scan yields bounded local-only evidence with `source_scan_incomplete` and `truncated=true`; a failed final revalidation yields empty hits with `source_changed` and `truncated=true`. When a loader is not injected, skip the corresponding signal deliberately without logging an error.
2. Aggregate eight normalized integer signals (`0..1000`) for each candidate in `retrieval_ranking.py`. Exact symbol: `1000` for case-sensitive full qualified lookup, `850` for qualified suffix, `700` for simple selector, else `0`; test against bounded exact tokens and retain duplicate names by distinct ID. Use descending precedence within each signal:
   - `name`: file exact path `1000`, module/basename `850`, exact stem `750`, otherwise `min(700, 700*matched_distinct_primary//max(1, number_of_primary_terms))` from case-folded identifier components of file path or definition name (not substring). A symbol uses only its own name for coverage, not path-only matches. File exact-symbol value is the strongest definition in its file.
   - `lexical`: full phrase `1000`, otherwise `min(950, 800*matched_primary//max(1,count_primary) + 150*matched_expanded//max(1,count_expanded))`. Load checked/live definitions before the lexical pass. For each path sort definition starts once and advance through the spans in line order as `on_match` arrives; choose the smallest covering definition (ties by start, then ID). Keep only the union of masks, phrase flag and best anchor per definition; file anchor caps must not change symbol attribution. Do not scan every definition per matching line or promote all siblings on file evidence alone.
   - `import_dependency`, `reverse_dependency`, `related_test`, `git_diff`: `1000` if evidenced; else `0`. `map_importance`: `min(1000, 125*incoming + 50*outgoing)` from unique checked graph neighbors. Only apply file-derived bonuses to an independently matched symbol; never create a symbol candidate from a file-level bonus.
3. Determine direct candidates by positive weighted `exact_symbol`, `name`, or `lexical`. Pick at most eight direct seed paths by descending *unrounded* direct weighted points then path. Expand once from that fixed seed list: per seed take at most eight sorted eligible outgoing targets, eight incoming importers, and eight `matching_tests` paths. Expansion does not recurse or add terms. Add relation/test-only file candidates only when that signal's weight is positive. Git and importance cannot qualify a result, cannot form seeds, and are skipped for Git weight zero or no candidates. Store up to eight unique dependency records per hit, favoring links to/from seeds before remaining edges, then direction, related path, importer range. A reverse record points to the source that imports the hit; do not present import-site coordinates as belonging to the target. Memoize by path, merge flags with set/max semantics. Bound matching tests by grouping existing filename matches per stem rather than repeatedly walking the checkout; count overflow as truncated.
4. Adjust configured weights by task kind before normalization: `feature` unchanged, `bugfix` lexical and Git ×2, `refactor` both import directions ×2, `test` related tests and reverse ×2, `diagnose` lexical and related tests ×2. All other factors are 1; clamp nothing after multiplying. `weighted_points=value*effective_weight`; score is `sum(points)//sum(all effective weights)` with unavailable signals treated as zero rather than renormalizing. Reasons are nonzero weighted contributions in the published field order. Sort by descending unrounded points, then descending **weighted** exact and name points, symbol before file, cheaper cost, path, start line (null first), end line (null first), ID. A zero-weight signal does not influence seed order, qualification, tie-breaks, source-window choice or cost. Dependency provenance can still describe checked static facts even when their ranking weight is zero. Ordering is stable across hash and filesystem iteration orders; different cache states may legitimately differ.
5. Use one file candidate per path. For a positive weighted lexical match choose its best anchor (Task 2 order) and at most 3 lines each side; other nonempty file candidates reference the first five lines. A definition references at most 40 contiguous lines: start at definition start for positive weighted exact/name evidence; otherwise use the best contained lexical anchor with up to three lines of lead-in, clipped to the definition. Short definitions may fit in full; never promote a whole-file candidate body. Apply `min(12000, per_file_chars_max)` to the canonical window at whole-line boundaries, trimming the farthest boundary first (trailing boundary on a tie), never dropping the anchor. If a single anchor line exceeds the cap, return its coordinates plus the estimate for that one line (nothing is emitted). Empty-file cost is 0; otherwise `estimate_tokens` of the exact LF-joined source slice with no added trailing newline, not a budget reservation. Use request-local per-path lines and cost memo.
6. Rank once and filter by requested result kind. In rank order, keep the first of each path (file) or ID (symbol). In `all` mode skip a later file/symbol row whose window overlaps an already selected row of the other kind on the same path; an unselected symbol must never suppress a file. For distinct symbols keep nested overlaps but skip an identical range already selected on the same path. Select through the first `top_k` rows that survive; detect truncation by whether one more nonduplicate row would survive. Capped expansion/dependencies/query terms/file anchors or a partial scan independently set `truncated`. Ordinary range clipping and duplicate suppression do not.

### Required Interfaces and Contracts

- `retrieve()` returns metadata only. A digest binds lines to captured canonical text; it is not a guarantee about a later edit. No prompt construction, provider calls, repo writes, persisted ranking cache or automatic index maintenance.
- No graph or Git read is needed for an empty corpus; no early empty return may bypass witness handling for a present scan. Filter any unsupported/unsafe path or qualified name before result serialization; never relax admission to increase recall.

### Requirement Evidence

- Original Request 1–16, expected fields, token efficiency and acceptance; reconciliation **Tasks and selection**, **Checked index**, **Fallback**, **Git evidence**.

### Error Handling and Logging

- Use fixed status codes, deterministic empty results and existing validation error surface. DEBUG one result event with task kind, source counts, returned count, cap flags and degradation codes only. WARN once on a changed witness or unexpected unavailable input; missing optional cache/Git normally DEBUG. No rendered source, reason snippets, input patterns or denied paths in logs.

### Tests

- `test_retrieval_ranking.py`: hand-calculated reason/score goldens; zero weights cannot change ties or range cost; five profiles; exact case/suffix/duplicate IDs; ninth-or-later file match must still find its definition; lexical-vs-filename distractor; both import directions, unrelated changed/high-degree decoys, related tests, seed/neighbor caps and no second hop; stable ordering under permutations; filtered file/symbol top-k; unselected symbols cannot suppress file windows, selected duplicate windows are skipped; empty/non-ASCII cost and long lines.
- `test_retriever.py`: current, missing, stale, unhealthy and incomplete graph; stable partial source scan proves no graph/Git calls or false optional-input errors; broken Python and non-Python hits; file changed mid-query; same-path range rebound after edit; hidden/secrets absent from all fields and logs; one scan/lexical pass and at most one parse per fallback file, no parse on warm graph. Larger synthetic tree with a tiny cap must not affect bounded traversal.

### Acceptance Criteria

- All eight signals have labeled normalized contributions; both source and definition rows have the specified provenance and source cost. A degraded query remains useful without silently trusting stale data.

### Verification

`uv run --locked pytest -q tests/context/test_retriever.py tests/context/test_retrieval_ranking.py tests/repository_map/test_retrieval_graph.py` — all pass.

## Phase Risks and Mitigations

- Cache freshness checks must not accidentally admit non-Python paths as index rows. Compare only the Python projection and require strict witness and checkout agreement.
- A file can have a high graph degree without task evidence. Git and map importance are boosts only; one-hop fixed seeds bound expansion and prevent feedback.

## Phase Completion Checklist

- Both tasks meet their acceptance criteria and verification commands pass.
- Update only the task checkboxes in `index.md`.
