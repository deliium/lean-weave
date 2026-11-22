# Phase 2: Bounded task-local expansion

Plan: [index.md](index.md)  
Tasks: 4–5  
Depends on: Phase 1 / Task 3

## Objective

Offer explicit L2 follow-ups that reuse prior provenance, reject stale references, and charge only newly supplied source to the cumulative context inventory.

## Current-Code Evidence

| Path | Symbols | Why it matters |
|---|---|---|
| `src/lean_weave/context/repository.py` | `SourceScan`, `FileSnapshot` | Open a fresh strict scan per expansion; canonical digest, not executor raw-byte revision. |
| `src/lean_weave/context/retrieval_models.py` | `DependencyEvidence` | Provenance includes importer range and digest; candidate edges need verification. |
| `src/lean_weave/repository_map/models.py`, `graph.py` | `DependencyGraph`, `ImportRelation`, `_edge_worthy` | Adjacency includes unresolved-member base edges; expansion must require `status == "resolved"`. |
| `src/lean_weave/context/python_index.py` | `extract_python_facts` | Source-based definition and import coordinates for deterministic resolution. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `src/lean_weave/context/packing_models.py`, `packing_source.py`, `packer.py` | modify | Typed expansion requests and resolvers, task session entry point. |
| `src/lean_weave/context/packing_session.py` | create | Opaque task state, revision checks, delivery and expansion limits. |
| `tests/context/test_packing_session.py`, `tests/context/test_packing_expansion.py` | create | Atomic transitions, source freshness, and exact L2 output. |

## Task 4: Track task inventory and reject stale references

### Intent
Prevent stale or cross-task references and unbounded follow-ups without storing source bodies between operations.

### Implementation Steps
1. Add `ContextPacker.open_task(request) -> (PackResult, PackingSession | None)`; create a session only after a ready result. `PackingSession` is opaque, single-owner and process-local. Store an immutable projection of task ID/query/specification, trusted system text, effective model/thinking, limits and output scope; never retain the original request object or its per-call envelope. Store checkout binding (resolved root, device and inode), frozen admission policy, returned map/source revision map, eligible ranked target refs and verified relation evidence, included fragment IDs and merged coverage intervals by `(path, digest)`, cumulative source units and attempts. Do not persist it, retain `SourceScan`, or store fragment bodies or transcripts.
2. `ContextPacker.expand(session, request, *, envelope=None) -> PackResult` accepts a fresh per-call envelope; `None` means no tools, replay, or structured response. Model, thinking defaults, system/task text and limits are fixed by the task settings; validate fresh replay and the fixed envelope before scanning. Reject foreign/closed sessions, malformed or unknown references, a caller-supplied digest that disagrees with the registered ref, and attempts beyond limit before scanning. A bad caller reference is a sanitized request error, not observed checkout drift; it does not close the session. Charge one attempt for each validated reference admitted to expansion (including insufficient, stale and no-change). Expose attempts and remaining expansion allowance. Stage changes and thread sharing are unsupported; no automatic retries.
3. Use a new strict scan; compare checkout binding, frozen admission policy, every previously emitted path revision, and the requested target/edge witness against current admitted snapshots. Recheck the open scan before publishing. Checkout/policy drift, a changed recorded digest, or a recorded path no longer admitted on a complete scan invalidates the session; return `stale_reference`, no prompt. If an incomplete scan cannot verify recorded paths, return `insufficient_context` with `source_scan_incomplete`, do not guess or update coverage. A changed final scan witness returns stale and closes the session. Never auto-refresh a stale session or reinterpret the selector against new content.
4. On a successful expansion atomically add covered IDs/ranges and charge only the newly supplied rendered source blocks to the stable cumulative budget; count initial blocks the same way. Store each exact fragment ref by ID in addition to the merged coverage. Keep a bounded private delivery record per successful expansion (at most `max_expansions`): opaque session-owned receipt, effective target, exact delta IDs, IDs needed to cover the target, normalized-envelope fingerprint and source/edge refs. Do not retain replay or bodies in that record. On failed/no-change attempts preserve inventory and cumulative cost. `close()` releases references. Expansion results include prior and newly included IDs; repeating a still-verified covered selector returns `no_change` without dispatchable messages.

### Required Interfaces and Contracts
- An expansion is a caller action, not a model tool or an orchestrator action. Stage APIs and workflow checkpoint/ledger remain unchanged.
- Do not treat canonical revisions as raw-byte revisions or global Git generations. Canonically equivalent newline changes between calls are not stale by themselves. A changed `.gitignore` that changes a tracked reference's admission is detected by the fresh scan; do not claim the frozen policy hashes every ignore file between calls.

### Requirement Evidence
Original Request 12, 14–19 and metadata; V2-01 strict admission and V2-03 checked retrieval. Verify foreign, stale, blocked, no-change and ready state transitions independently.

### Error Handling and Logging
DEBUG numeric counters and safe reason enums; WARN once when a session is invalidated for stale source/policy; no task query, selector, source text or raw diagnostics in logs.

### Tests
Exact attempt limit including unsuccessful attempts, zero attempts, closed/foreign session, same ID in another checkout, caller digest typo vs actual changed/deleted source, ignore-policy admission change, incomplete scan with missing witness, mutation during final revalidation, repeated identical target, unchanged unrelated edit, and no source or tool replay retained.

### Acceptance Criteria
No follow-up sends stale source or advances committed inventory on failure.

### Verification
`uv run --locked pytest tests/context/test_packing_session.py` — caps, transition and revision cases pass.

## Task 5: Resolve explicit L2 targets without silently clipping

### Intent
Make follow-up selection useful while avoiding repetition and unconstrained exploration.

### Implementation Steps
1. Add a discriminated `ExpansionRequest` with exactly one selector: symbol (`path`, expected digest, qualified name, optional occurrence), range (known `path`, expected digest, inclusive start/end), or dependency (published edge ID). Build the edge ID deterministically from direction, anchor path/digest, related path/digest, and importer path/digest and inclusive source coordinates. Require exactly one matching fully resolved `ImportRelation` when publishing that witness. Full-file uses the range variant with a `full_file` flag and a nonblank bounded justification; require coordinates to match the exact admitted file extent. Bind symbol/range to an emitted or bounded candidate path ref; a dependency may introduce one new path only through its recorded edge. Apply the same output allowlist and admission checks.
2. Resolve symbols to complete decorator-inclusive definitions. For a Python explicit range, promote to the smallest complete AST statement or contiguous adjacent complete siblings in the same suite covering the request; if none covers the range, return `invalid_structural_range` rather than silently expanding to the full file. An explicit full-file request uses the complete canonical file. For other admitted text, emit exactly the requested lines. For dependencies, match the same fully resolved edge and both endpoint digests on the fresh scan; reject unresolved-member, ambiguous and direction mismatches. Select the first original ranked related definition for that path if present, otherwise its earliest complete top-level statement. If the graph is missing or stale, return `dependency_unavailable` unless observed endpoint change already proves staleness. No degraded live dependency inference and no transitive traversal; cold sessions still allow symbol/range expansion.
3. Compare target coverage with inventory. If wholly covered return `no_change`; if no overlap, trial the complete target. For partial overlap, subtract covered lines and permit the delta only when each remaining interval is itself a complete admissible structural unit (non-Python whole lines), and the union of prior + delta covers the entire requested target. Emit ordered units labeled as delta with covered-reference metadata. Do not emit half a definition to save tokens, repeat its signature, or automatically resend an overlapping full file. If overlap cannot be resolved structurally, return `insufficient_context` with `overlap_requires_explicit_rebuild`; a caller may start a new task session, never a silent reset. Fit all new units atomically under current section, request, cumulative, and character caps; no unsolicited neighbors or optional filler.
4. A result's expansion delta is for a caller's task inventory and is not proof of model memory. Task 6 requires a checked delivery operation, even for a standalone delta; for an overlapping target, include the exact prior coverage IDs or refuse dispatch. Never automatically attach all previous code, a prior map, or old tool outputs.

### Required Interfaces and Contracts
- L0 or L1 can expand to L2. L2 does not become a new discovery mode. Ambiguous symbol, unavailable dependency, out-of-bounds range, empty target and exceeded budgets are explicit typed outcomes.
- Every newly included fragment has actual materialized coordinates and a stable ID. Requested range and effective structural range may differ; expose both, never fabricate line or definition text.

### Requirement Evidence
Original Request L2, 8–18; V2-02 graph resolution and V2-03 evidence provenance. Verify symbol, range, justified full-file, dependency, and missing/ambiguous/overlap cases.

### Error Handling and Logging
DEBUG selector kind, safe reason code, units and remaining caps only. Missing or unresolved dependencies and optional local graph absence are normal insufficiency, not exceptions or reasons to widen search.

### Tests
Unique/ambiguous/decorated symbol, exact and promoted range, unresolved-member edge with target ID, graph missing after a known edge, changed related endpoint, wrong direction, justified whole file from L0, forbidden whole file, structurally viable and nonviable overlap, giant function, Unicode/no trailing newline, exact cumulative fit/one over, and an atomic multi-unit failure.

### Acceptance Criteria
Every successful L2 addition is bounded, explicitly requested, revision-checked and complete; no repeated automatically supplied source.

### Verification
`uv run --locked pytest tests/context/test_packing_expansion.py tests/context/test_packing_session.py` — source and budget invariants pass.

## Phase Risks and Mitigations

- Stateless model requests cannot rely on previous context: distinguish caller inventory and explicitly supplied model input.
- A relevant source unit can be larger than the budget: return typed insufficiency rather than pretend a partial body is complete.

## Phase Completion Checklist

- Tasks 4–5 satisfy their acceptance criteria and verification passes.
- Update progress only in `index.md` after each verified task.
