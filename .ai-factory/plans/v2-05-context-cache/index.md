<!-- aif:plan-mode:ultra -->
# Ultra Implementation Plan: Incremental Context Cache and Reuse

Plan ID: `v2-05-context-cache`  
Mode: ultra  
Branch: `main` (planning checkout unchanged; select this bundle explicitly for implementation)  
Created: 2026-11-22  
Depends on: [v2-01-code-indexer](../v2-01-code-indexer/index.md), [v2-02-repository-map](../v2-02-repository-map/index.md), [v2-03-context-ranking](../v2-03-context-ranking/index.md), and [v2-04-context-budget](../v2-04-context-budget/index.md) (all task ledgers complete).

## Original Request

Plan ID: v2-05-context-cache
Plan Title: Incremental Context Cache and Reuse
Milestone: V2 - Context Intelligence

Dependencies:
- v2-01-code-indexer
- v2-02-repository-map
- v2-03-context-ranking
- v2-04-context-budget

Goal:
Implement reliable caching of deterministic repository analysis and context construction results across feature executions.

Architecture:
- Reuse existing SQLite infrastructure where appropriate.
- Reuse CodeIndex and ContextProvider.
- Preserve worktree isolation.
- Keep cache behavior independent of LangGraph and DeepSeek.

Requirements:

1. Implement a versioned ContextCache interface.
2. Cache reusable deterministic artifacts:
   - parsed symbol metadata
   - module dependency information
   - repository map fragments
   - retrieval candidates
   - ranking results
   - context pack components
3. Use source hashes for cache validation.
4. Include relevant configuration and algorithm versions in cache keys.
5. Include task-specific inputs when caching retrieval results.
6. Detect relevant source changes.
7. Invalidate stale entries.
8. Support dependency-aware invalidation where necessary.
9. Avoid invalidating unrelated cached data.
10. Support safe concurrent reads.
11. Use atomic writes or transactions.
12. Detect corrupted cache entries.
13. Support versioned cache migrations.
14. Limit cache storage growth.
15. Support explicit clearing and rebuilding.
16. Preserve source revision information.
17. Prevent unauthorized cross-worktree data reuse.
18. Permit content-addressed reuse across worktrees only when source identity, permissions, and configuration are validated.
19. Record cache hits, misses, rebuilds, and invalidations.
20. Preserve existing context retrieval behavior when caching is disabled.

CLI:
- wl cache status
- wl cache clear
- wl cache rebuild
- wl cache stats

Token-efficiency requirements:
- Cache operations require no LLM calls.
- Avoid unnecessary repeated context analysis.
- Do not cache arbitrary model-generated code or reasoning.
- Do not reuse stale context fragments.
- Measure computational cache benefits separately from API token savings.
- Do not assume local cache hits reduce transmitted tokens.

Testing:
- Cache hits and misses
- Source modifications
- Dependency changes
- Configuration changes
- Invalid entries
- Corrupted database recovery
- Worktree isolation
- Cache disabled mode
- Concurrent access

Acceptance criteria:
- Unchanged analysis results are reused.
- Stale results are invalidated correctly.
- Cache storage remains bounded.
- Worktree isolation is preserved.
- Existing ContextProvider behavior remains compatible.
- No additional LLM calls are introduced.

Out of scope:
- Redis
- Distributed caches
- Model response caching
- Cross-project semantic memory
- Background cache services

Save using the exact plan ID:
v2-05-context-cache

## Settings

- Testing: yes
- Logging: verbose
- Docs: yes

## Roadmap Linkage

Milestone: "V2 - Context Intelligence"  
Rationale: This follow-on reuses the completed milestone's index, map, ranking and packing work without reopening or changing its completed status.

## Requirements Reconciliation

Authority: Original Request defines the new behavior, `.ai-factory/RULES.md` defines prompt/state boundaries, and `.ai-factory/ARCHITECTURE.md` and the four dependency bundles define the existing contracts. The roadmap links scope, not new semantics. The requirement for opt-in peer reuse refines checkout isolation: the legacy index remains isolated, and only a directly authorized new facts lookup is shareable. No new stage starts task-aware retrieval or packing.

| Decision / supported combination | Source and section | Verification evidence |
|---|---|---|
| Disabled mode preserves existing behavior, including legacy index reads. Status/stats are no-I/O; only confirmed clear can touch the disabled cache namespace. Plan/verify never load it. | Request 17, 20, CLI; Rules stage isolation; Architecture Workflow and CodeIndex | No-I/O spies, injected provider, exact disabled/cold/warm output, CLI exit tests (Tasks 1, 4, 10–11). |
| Enabled, current, private healthy store: exact key/proof yields metadata hit and reuses computation; missing, stale, corrupt or busy falls back to the pre-existing live behavior. Changed/incomplete witnesses and stale post-maintenance batches never publish. | Request 3–14, 16, 19; v2-01 checked lookup; v2-03/v2-04 final witnesses | Concurrency, maintenance epoch, failed witness, bounded space, corruption and fallback (Tasks 2–3, 5, 7–8, 10). |
| Source edits invalidate matching content-dependent rows and importer watchers; task/scope/config changes match only exact semantic inputs. Unrelated file artifacts remain reusable; graph adjacency, prompt framing and packing budgets remain live. | Request 2–9, 16; v2-02 incremental graph, v2-03 ranking, v2-04 packing | Add/remove/shadow package; compare edited `multi_file_banner` from `benchmarks/fixtures/retrieval/cases.json` to uncached (Tasks 5–8, 11). |
| Same checkout is the default; different worktree is a miss even at equal HEAD. Only a trusted same-project peer pair with matching live source/config/permission may use path-local facts; no credential, state or graph sharing. | Request 17–18; Rules stage scope; Architecture worktree boundary | Real linked worktrees verify default isolation, approved hit, read-only peer and rejection matrix (Tasks 4, 9, 11). |
| Known older schema may migrate; unknown schema is not overwritten; confirmed clear or explicit rebuild recovers validated corrupt storage. Rebuild never edits the index and does not precompute arbitrary tasks. | Request 10–15, 19, CLI; Architecture opt-in index maintenance | SQLite fixtures and CLI maintenance tests (Tasks 2–3, 10). |
| Cold/warm/disabled computations are compared offline with and without the legacy index as separate workloads; outputs and existing LLM calls stay equivalent within each. Local time/call counts are separate from transmitted tokens. | Request token-efficiency, acceptance and out-of-scope; Rules local verification | Real-corpus benchmark plus mock-gateway feature comparison (Task 11). |

## Architecture and Decisions

- Use a dedicated, private external SQLite store keyed to checkout path/device/inode. Share identity helpers, defensive SQLite patterns and the CodeIndex facts codec; keep both stores, schema versions and maintenance commands separate.
- Compute logical keys from kind/version/config/normalized inputs and validate exact live source/dependency proofs on every use. Persist only bounded deterministic metadata. Keep repository admission and prompt budgets authoritative; no full prompts, source bodies, raw tasks, model-generated responses, or L2 session state in storage.
- Unchanged facts, importer relations, map rows, base candidates, rank projections and spans can be reused independently. Fresh reads, scoped filtering, Git/lexical backend checks and final plan/pack/witness guards remain live where they are already part of the contract.
- Rollout is opt-in via product `context.cache.enabled`, not AI Factory config. No LangGraph/DeepSeek dependency, automatic index rebuild, background work, global shared store, or API token-savings claim. No blocking open questions.

## Phase Index

1. [Phase 1: Cache contracts and storage](phase-01-storage.md) — Tasks 1–3
2. [Phase 2: Incremental source analysis](phase-02-analysis.md) — Tasks 4–6
3. [Phase 3: Context reuse and cross-worktree boundary](phase-03-context.md) — Tasks 7–9
4. [Phase 4: Operations, acceptance and documentation](phase-04-operations.md) — Tasks 10–12

## Cross-Phase Dependencies

- Tasks 4–6 require Tasks 1–3 for the key/proof, storage and maintenance contracts.
- Task 7 requires Tasks 4–5 for facts and scan-bound graph lookup; Task 8 requires Tasks 6–7 for the shared header and rank batch; Task 9 requires Tasks 4 and 7 for facts and the trusted retrieval entry point.
- Task 10 requires Tasks 3, 6 and 8 for bounded seed and maintenance; Task 11 requires Tasks 9 and 10 to cover authorized peers and maintenance behavior. Task 12 documents verified behavior after Task 11.

## Tasks

### Phase 1: Cache contracts and storage

- [x] Task 1: Define cache contracts and namespace ([details](phase-01-storage.md#task-1-define-cache-contracts-and-namespace))
- [x] Task 2: Implement bounded transactional storage ([details](phase-01-storage.md#task-2-implement-bounded-transactional-storage)) (depends on 1)
- [x] Task 3: Implement maintenance and recovery ([details](phase-01-storage.md#task-3-implement-maintenance-and-recovery)) (depends on 2)

### Phase 2: Incremental source analysis

- [x] Task 4: Reuse admitted Python facts ([details](phase-02-analysis.md#task-4-reuse-admitted-python-facts)) (depends on 3)
- [x] Task 5: Reuse dependency resolutions ([details](phase-02-analysis.md#task-5-reuse-dependency-resolutions)) (depends on 4)
- [x] Task 6: Reuse repository map fragments ([details](phase-02-analysis.md#task-6-reuse-repository-map-fragments)) (depends on 5)

### Phase 3: Context reuse and cross-worktree boundary

- [x] Task 7: Reuse retrieval candidates and ranking ([details](phase-03-context.md#task-7-reuse-retrieval-candidates-and-ranking)) (depends on 4, 5)
- [x] Task 8: Reuse packing components ([details](phase-03-context.md#task-8-reuse-packing-components)) (depends on 6, 7)
- [x] Task 9: Gate explicit content-addressed peer reuse ([details](phase-03-context.md#task-9-gate-explicit-content-addressed-peer-reuse)) (depends on 4, 7)

### Phase 4: Operations, acceptance and documentation

- [x] Task 10: Expose offline cache maintenance ([details](phase-04-operations.md#task-10-expose-offline-cache-maintenance)) (depends on 3, 6, 8)
- [x] Task 11: Verify behavior and measure local reuse ([details](phase-04-operations.md#task-11-verify-behavior-and-measure-local-reuse)) (depends on 9, 10)
- [x] Task 12: Document configuration and operations ([details](phase-04-operations.md#task-12-document-configuration-and-operations)) (depends on 11)

## Commit Plan

- **Commit 1** (after Tasks 1–3): `feat(cache): add bounded checkout-local context storage`
- **Commit 2** (after Tasks 4–6): `feat(cache): reuse admitted facts and incremental repository analysis`
- **Commit 3** (after Tasks 7–9): `feat(cache): reuse retrieval and packing work with isolated peer access`
- **Commit 4** (after Tasks 10–12): `feat(cli): add offline context cache operations and verification`

## Definition of Done

- The six artifact kinds can skip unchanged deterministic work; changed sources, dependencies and applicable settings cannot reuse a stale result.
- Disabled, cold and warm outputs agree; stage prompt compatibility, worktree boundaries, LLM-call counts and active safety checks remain intact.
- SQLite concurrency, limits, migration, deliberate corruption and explicit recovery pass the offline suite; all four commands work without LLM access.
- The checked-in fixture benchmark reports local time and work counters separately from token estimates or actual provider usage. The mandatory documentation checkpoint is complete.
