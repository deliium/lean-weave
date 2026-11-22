# Phase 1: Deterministic initial packing

Plan: [index.md](index.md)  
Tasks: 1–3  
Depends on: completed V2-01, V2-02, V2-03 and V1-03

## Objective

Produce offline L0/L1 packs from ranked, revision-checked source under explicit section and request limits, without changing the V1 builder.

## Current-Code Evidence

| Path | Symbols | Why it matters |
|---|---|---|
| `src/lean_weave/context/models.py`, `builder.py`, `render.py` | `ContextPack`, `ContextBuilder`, V1 renderer | Preserve schema, output and prompt construction; V2 must not reuse V1 clipping/omission as an integrity guarantee. |
| `src/lean_weave/context/retriever.py`, `retrieval_models.py` | `ContextRetriever.retrieve`, `_rank_local`, `RetrievalHit`, `DependencyEvidence` | Ranked hit windows are references, not complete source units. |
| `src/lean_weave/context/repository.py`, `python_index.py` | `scan_retrieval_sources`, `SourceScan`, `FileSnapshot`, `extract_python_facts` | Strict admission and checked text/coordinates; decorators and definition extents matter. |
| `src/lean_weave/repository_map/service.py`, `render.py` | `graph_for_retrieval_scan`, `module_row` | Optional graph from the same scan; whole module rows can be reused without leaking signatures. |
| `src/lean_weave/config/models.py`, `metrics/budget.py` | `ContextConfig`, `LLMConfig`, `estimate_request_tokens` | Strict config, existing 1024 output default, and separate preexisting gateway reservation estimation. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `src/lean_weave/config/models.py` | modify | Add opt-in packed-context limits. |
| `src/lean_weave/context/packing_models.py`, `packing_tokens.py` | create | Versioned contract, rendering/counting inputs, local budget estimator. |
| `src/lean_weave/contracts/deepseek_request.py`, `src/lean_weave/providers/deepseek.py` | create/modify | Extract shared pure request normalization and wire mapping; preserve the existing provider's behavior. |
| `src/lean_weave/context/retriever.py` | modify | Internal checked callback over the existing scan/rank pipeline. |
| `src/lean_weave/context/packing_source.py`, `packer.py` | create | Source catalog, structural fragments and deterministic initial selection. |
| `tests/config/test_context_config.py`, `tests/context/test_packing_tokens.py`, `test_packing_source.py`, `test_packer.py` (latter three under `tests/context/`) | modify/create | Exact-budget, snapshot, integrity and selection coverage. |

## Task 1: Define pack contracts and token accounting

### Intent
Make every composition decision inspectable, and keep local counts distinct from API usage.

### Implementation Steps
1. Add strict `ContextConfig.packing: PackingConfig` with `cli_default=false`, `total_tokens=8192`, `system_tokens=1024`, `task_tokens=1024`, `map_tokens=512`, `source_tokens=4096`, `tool_tokens=2048`, `output_tokens=1024`, `max_expansions=2`, and `cumulative_source_tokens=8192`. Use strict integers: total/cumulative 1–1,000,000, section caps 0–1,000,000, output 1–1,000,000, expansion count 0–16. Require output < total, each input cap ≤ total − output, and cumulative ≥ source cap. Section caps need not add up to total; all constraints apply independently. Existing scan, code-character and map-row ceilings still apply. Do not migrate or rewrite old YAML.
2. Introduce frozen `PackRequest`, `PackedContextV2`, `PackResult`, `FragmentRef`, `PackedFragment`, and `PackMetrics`. Use immutable tuples and defensive copies of JSON-shaped data. Request has task ID (bounded opaque identifier), retrieval query, separate task specification, trusted system instructions, L0/L1, optional exact required symbol selectors, optional output path allowlist, and typed envelope (tool definitions, bounded legal same-task tool replay, response-format and model options). This is an explicit whole-checkout read preview; the output allowlist does not restrict pre-ranking reads and is not stage read approval. No implicit history, credentials, scratch state or tool approval in the envelope.
3. Include `version=2`, level, task ID, ordered fragments/map rows, ordered omission/reason codes, per-fragment reference (`path`, inclusive lines, canonical source digest and kind), selection reasons (ranking/dominant signal or explicit expansion), included fragment IDs, prior IDs where applicable, source revisions, section counts/limits, whole-input count, output reserve, total and remaining room, estimator kind, and composition counters (considered, selected, omitted by reason, deduplicated, expansion count and cumulative source units). Derive fragment ID by SHA-256 of canonical JSON `[path, digest, start, end]`; kind and rationale cannot change identity. `PackResult` statuses: `ready`, `insufficient_context`, and `stale_reference`; a non-ready result may expose safe metadata and omissions, but never deliverable prompt messages. Expansion adds `no_change` in Task 4.
4. In `packing_tokens.py`, count canonical sections independently and the final composed request separately, including framing, options, tool schemas, replay messages (calls/reasoning/results together), and structured response format. The system section is only trusted system text; task, map and source remain distinct labeled blocks in the first user message. Use canonical, compact, UTF-8 JSON (`ensure_ascii=False`, deterministic key order, strict JSON-compatible data). Include labels and fragment headers in section counts; represent absent sections as zero. Count tool/schema/replay content in the tool section and include it once in the assembled input. Never derive total solely by summing section counts; add the requested completion cap after whole-input estimation.
5. Define injected `TokenCounter` with section count and whole-request count, and explicit capability check for effective model, chat template and present wire fields. Use compatible, already-available local tooling only; no download, generic tokenizer substitution, or new mandatory dependency. The locked runtime currently ships no verified DeepSeek tokenizer, so the default registry is empty and the explicit caller can provide a compatible adapter. For unsupported shapes or invalid counts/ordinary adapter failure, restart the composition once using conservative UTF-8 serialized bytes (one token per byte) plus 64 fixed framing units for the whole request. Count even accurate local results as `local_tokenizer`, never `reported`; the byte mode is `conservative_estimate`, not a server guarantee. Cumulative source limits always use the canonical serialized byte-unit counter for comparability across calls and estimator fallback. Bound/canonicalize envelope input before calling the counter.
6. Extract pure input normalization and wire projection from `DeepSeekProvider._prepare` and `_wire_request` into `contracts/deepseek_request.py`; share model capabilities, thinking/response modes, tools/messages, output-cap validation and `extra_body` construction. Keep feature/stage attribution, retry policy, result-model validation and transport/journal at the gateway. Packing calls the projection on every rendered trial and uses normalized replay for section counts. Offline preview passes its own declared output ceiling without clamping; the gateway still applies `LLMConfig.max_output_tokens` at dispatch. Unsupported model/options or illegal replay are validation errors, not tokenizer fallback. Keep the legacy provider validation order, SDK arguments and `MODEL_CAPABILITIES` export. No provider instance or client is needed to use the helper.
7. For packed input, accept only complete assistant tool-call batches and matching tool results, in order, with unique nonempty call IDs; reject orphan or duplicate results, missing results, or non-replay roles in either thinking mode before any source scan. Require the caller to supply only same-task authorized replay; do not infer task identity from tool text. Count the existing provider-normalized shape (including serialized argument strings and reasoning when present); V1 replay validation is unchanged.

### Required Interfaces and Contracts
- `ContextPack`, `ContextQuery`, `ContextProvider.build` and the V1 renderer remain unchanged. V2 is a separate, explicit consumer interface and has no durable schema changes.
- Treat equality at any limit as a fit. No implicit borrowing between sections. Output reservation is never available for source. `remaining_total = max(0, total_tokens − estimated_input − reserved_output)`; give section headroom separately.
- Local input is deterministic for equal repository snapshot, request, config and counter. CLI task IDs may be deterministic SHA-256 of the validated query; identity also includes the in-memory session, never hash alone.

### Requirement Evidence
Original Request 3–6, 19–20 and metadata; `.ai-factory/RULES.md` stage isolation and budgets. The compatible-tokenizer, missing/incompatible-tooling and counter-failure rows all exercise identical composition checks.

### Error Handling and Logging
Configuration/type errors use existing sanitized validation. Required system/task/tool envelopes over cap return `insufficient_context` with the failing section(s); no source or credentials in logs. DEBUG logs count method, fallback reason enum and numeric headroom only.

### Tests
Cover strict nested config, legacy config defaults, zero optional caps, independent caps, exact fit/one over, multibyte Unicode, JSON escapes, output reservation, replay overhead, absent tools, mismatched or unsupported model, invalid tokenizer output, and mid-composition fallback with no mixed counters. Include thinking on/off, orphan/duplicate/missing tool results, argument mappings normalized to strings, and optional JSON mode. Use a deterministic injected counter; no network.

### Acceptance Criteria
Every section and the whole input have separately testable limits; no local estimate is presented as provider usage.

### Verification
`uv run --locked pytest tests/config/test_context_config.py tests/context/test_packing_tokens.py tests/providers/test_deepseek.py` — boundary, projection parity and compatibility cases pass.

## Task 2: Bind ranked candidates to checked source

### Intent
Materialize source from the same admitted snapshot used for ranking; never trust cached excerpts or coordinates without verification.

### Implementation Steps
1. Factor the existing `ContextRetriever.retrieve` scan/rank body into a shared internal path; add an internal generic callback taking the ranked `RetrievalResult`, open `SourceScan`, and checked graph or `None`. Invoke before final witness verification; discard callback output on a changed witness. In the new packing branch catch `ContextScanError` at context-manager entry as well as acquisition, and return safe `source_unavailable` metadata. Keep existing offline retrieve result, degraded ordering, logs and empty/unavailable behavior unchanged. Do not return a live scanner or retain snapshots after the operation.
2. In `packing_source.py` build an operation-local path→snapshot catalog from the strict scan. In the packing-only rank path, restrict candidate states and dependency endpoints to the exact normalized output allowlist before ranking and `dedupe_and_select` consume top-K. Filter map and metadata as well; `None` allows all admitted paths and an explicit empty set allows none. Preserve the full scope and results of public `retrieve()`. Never treat the index, a hit path, graph edge, or selector as admission. For every materialized hit verify digest, inclusive coordinates and source kind. Verify graph provenance against admitted revisions; adjacency or retrieval evidence alone is not proof of a resolved edge.
3. Source units: resolve by path/current digest plus retriever definition start and qualified name, using the existing `DefinitionFact` coordinate convention for matching but separate full-line structural spans for rendering. For explicit selectors, number same-qualified-name matches in `(start, end, traversal_order)` order across kinds; reject ambiguity when no occurrence is supplied. For a Python file hit, pass `RankCandidate._anchor` in private callback data (not public hit JSON); use the actual matched line, or the first complete top-level statement for a name-only hit. Resolve to the smallest complete enclosing definition, otherwise the smallest complete enclosing statement. Include the leading `@` line even if the AST decorator expression begins later; use token spans plus AST, preserve physical lines, and group same-line semicolon siblings if required to avoid claiming only part of an emitted line. If an independently complete span cannot be proven, use metadata only. For non-Python file hits use the exact retrieval line window, labeled as a range, never a definition. Use `per_file_chars_max` as aggregate selected body characters per file and `total_code_chars_max` across the pack; never slice a source unit to fit.
4. Extract lines using existing LF canonical source; preserve indentation, Unicode, empty lines and EOF newline presence. Slice using line offsets into the snapshot rather than `.strip()` or reformatting; keep fragment headers separate from the source bytes. Map L0 uses checked module rows (paths and module labels only), ranked matched paths first, then lexical admitted Python module paths if the scan is complete; when the index is absent/stale, construct metadata-only module rows from admitted Python paths and expose degradation. No signature/definition body in map rows; apply existing `map_rows_max` and stricter of existing and V2 map token caps. Only ranked matched paths are eligible for map rows on incomplete scans.

### Required Interfaces and Contracts
- The internal callback is synchronous and read-only, and returns detached pack data only after witness success. Use `SourceScan.matches_retrieval_policy` and `revalidate` while the scan is open. No second independent raw read during initial composition.
- Files without an admitted body, mismatched digest/coordinates, or out-of-scope selectors cannot produce fragments. A missing index can degrade; a changed witness cannot. The packer must not build/update caches, call a model, execute repository code or widen scope.

### Requirement Evidence
Original Request 2, 7–11, 16–17; V2-01 checked admission and V2-02 checked graph, V2-03 `RetrievalHit`/`DependencyEvidence` contract. Cold/warm/degraded selection must always derive bytes from an admitted snapshot.

### Error Handling and Logging
Emit stable omission/degradation codes without file text, paths, exception payloads or query strings in logs. WARN on observed source change; DEBUG counts for candidate resolution, map fallback and filtered units.

### Tests
Use parenthesized multiline decorators, semicolon siblings, nested definitions, an actual match near the end of a clipped window, same-name different-kind definitions, non-Python and Unicode/CRLF fixtures, terminal newline/no-newline cases, secret and ignored paths, and injected witness/digest mismatch. Use `top_k=1` with an out-of-scope higher hit, and confirm the packing shortlist still finds the in-scope hit while `retrieve()` is unchanged. Exercise cold graph/live fallback and warm graph without writing caches during packing.

### Acceptance Criteria
Every emitted source/map item has current admitted provenance and the source excerpt is an exact contiguous canonical slice.

### Verification
`uv run --locked pytest tests/context/test_packing_source.py tests/context/test_retriever.py tests/repository_map/test_retrieval_graph.py` — witness rejection and old retrieval contracts pass.

## Task 3: Select and render initial context

### Intent
Deliver L0 metadata-only and L1 relevant source packs with atomic, explainable omissions.

### Implementation Steps
1. Implement `ContextPacker.build(request) -> PackResult` on the Task 2 callback. L0 selects fitting whole module rows only and is insufficient if none fit; reject explicit source requirements at L0. L1 uses the same map candidates and requires all explicit definitions (exact `path` + qualified name, occurrence required when ambiguous); resolve these directly in the admitted, allowed catalog even if they miss top-K. Otherwise use the first eligible ranked direct hit (positive exact/name/lexical weighted evidence) as the required anchor. If the selected anchor cannot be materialized or does not fit, return insufficient instead of substituting a cheaper optional hit; no direct hit is also insufficient.
2. Fit the required system/task/tool envelope before opening the scan. Then place required source before optional map/source allocation in the total budget. Normalize nested required units by retaining the complete containing unit and recording covered selectors. For optional units, compute full rendered costs, then order by descending `relevance_score / max(1, source-section cost)` using integer cross-products, breaking ties by original rank, path and coordinates. Admit complete units only, recomposing and recounting on each trial; enforce all caps, including conservative cumulative source units. After required units, try map rows in stable order, then optional source. Keep optional source only for ranked hits with positive direct or import/reverse-import weighted evidence; do not fill otherwise empty space.
3. Skip any optional unit overlapping an already selected unit, including a broader optional unit covering a required selection; record containment as deduplication and other overlap as omission rather than trimming. For two mandatory crossing units, accept a single complete enclosing structural unit only if it safely covers both and fits; otherwise return explicit insufficient. Render no separate signature for any covered definition. Metadata can retain selectors/reasons; prompt contains a function signature only where it occurs in the body. Label map and source as untrusted data in distinct blocks with collision-free delimiters, and count the final deterministic representation including headers. Do not insert ellipses or omission notes inside a fragment.
4. Preserve reasons, invalid/missing requirements and all limiting caps in result metadata. Publish digest-bearing typed map row refs, and expose bounded admitted candidate refs and verified one-hop relation evidence as metadata for explicit expansion, never as extra prompt material. A scan marked incomplete can yield a ready pack only if all required inputs were resolved and fit; preserve `source_scan_incomplete` and do not infer absent dependencies from incomplete data. Source witness failure invalidates the entire pack.

### Required Interfaces and Contracts
- Initial `build` returns no retained scan handle or session state. Task 4 will reuse this transaction for opening a session; the pure build output stays stable. A non-ready result must not be dispatchable.
- Fragment order and counts are deterministic. L1's map is optional when its budget is zero or essential source leaves no room. Required source cannot be traded for a better utilization score.

### Requirement Evidence
Original Request L0/L1, 1–2, 7–11, 18–20; `.ai-factory/ARCHITECTURE.md` Token strategy; V2-03 rank windows are not prompt excerpts. Verify ready, insufficient, and stale separately.

### Error Handling and Logging
Return stable reason codes such as `no_relevant_source`, `required_source_unavailable`, `required_source_over_budget`, and `map_unavailable`; no extra model calls or relaxed caps. DEBUG composition totals and omissions; INFO only high-level outcomes at CLI boundary, never source/query/system text.

### Tests
Exact-budget selection, stable relevance/cost ties, repeated runs, class/member overlap in both ranking orders, required overlap coverage, crossing optional overlap, L0 with no excerpts, L1 with zero map budget, high-ranked giant function with cheap unrelated body, missing required selector, unchanged source, and stale during final witness. Compare render body with canonical original bytes.

### Acceptance Criteria
Packs contain no overlapping source ranges or clipped definitions, and explicitly report required evidence that cannot fit.

### Verification
`uv run --locked pytest tests/context/test_packer.py tests/context/test_packing_source.py` — deterministic composition and insufficiency cases pass.

## Phase Risks and Mitigations

- Ranking snippets and AST coordinates have different extents: resolve units from the checked source, not from ranked window text.
- Conservative fallback can shrink selection: make it visible and fail for missing required evidence, rather than silently undercounting.

## Phase Completion Checklist

- Tasks 1–3 satisfy their acceptance criteria and verification passes.
- Update progress only in `index.md` after each verified task.
