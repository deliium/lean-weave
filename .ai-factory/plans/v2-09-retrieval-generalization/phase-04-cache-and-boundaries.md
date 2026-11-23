# Phase 4: Cache correctness and product boundaries

Plan: [index.md](index.md)  
Tasks: 5–6  
Depends on: Phase 3 / Tasks 3–4

## Objective

Guarantee fresh dependency evidence on warm runs and expose enabled behavior through existing V2 product entry points without increasing authority.

## Current-Code Evidence

| Path | Symbols | Why it matters |
|---|---|---|
| `src/lean_weave/context_cache/packing.py`, `src/lean_weave/context_cache/service.py` | packing spans, batch, live proof, epoch | Stored spans are file-local metadata; batch publication is fenced. |
| `src/lean_weave/workflow/context_lifecycle.py` | `_resolve_scope`, `_round`, `_read`, `context_authority_material` | Implement uses approved task/one-hop/test scope, retry uses attributed paths, and sealed rounds rebuild after owned edits. |
| `src/lean_weave/context/packing_gateway.py` | `prepare_packed_request` | Ready packs are re-counted/guarded through existing normalized wire path. |
| `src/lean_weave/cli/commands/context.py` | `_packed_payload`, `_packed_human`, `_emit_packed` | Build shows text; inspect shows metadata; budget shows aggregate counts. |
| `src/lean_weave/cli/commands/config.py` | `show` | Serializes effective config; the opt-in field appears there even though packed output remains unchanged when disabled. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `src/lean_weave/context/packer.py`, `src/lean_weave/context/dependency_coverage.py` | modify | Isolate proof lifetimes; continue using existing ContextCache adapters and batch ordering. |
| `tests/context_cache/test_packing.py`, `tests/context_cache/test_dependency_coverage.py` | extend/create | Enabled disabled/cold/warm parity and edit/collision/sibling tests. |
| `src/lean_weave/workflow/context_lifecycle.py`, `src/lean_weave/cli/commands/context.py` | modify | Authority policy, exact read-repack integration, opt-in diagnostic projections. |
| `tests/workflow/test_context_lifecycle.py`, `tests/workflow/test_context_expansion.py`, `tests/cli/test_context_packing.py` | extend | Sealed workflow, config drift, isolation and CLI compatibility regressions. |
| `tests/cli/test_dependency_config.py` | create | Actual YAML policy decoding and safe `wl config show` projection. |
| `docs/context-packing.md`, `docs/context-cache.md`, `docs/worktree-workflow.md` | modify | Run mandatory docs checkpoint through `/aif-docs`; document supported subset, caps, statuses, isolation and rollout. |

## Task 5: Prove fresh context on warm cache and worktrees

### Intent
Invalidate derived dependency context on importer, definition, presence or ambiguity changes while keeping reusable file-local facts cheap.

### Implementation Steps
1. Reuse existing ContextCache for checked Python facts and packing spans; use their existing keys/proofs/epoch. Do not persist `DependencyLink`, misses, parse trees, dependency traversal, ready packs, prompts, or receipts. Build a fresh scoped map from live snapshots for resolution each time; do not trust cached adjacency/rank graph or peer facts without the existing identity/digest/contract proof. Analyzer memoization must die with the checked scan.
2. On selection and expansion bind result to checked revisions, emit scope and lookup universe. Stage the new full-facts/span cache writes in the packer's batch and publish only after a ready, revalidated operation; don't call workflow `_facts_for` as the new proof loader since it flushes independently. Existing preselection/ranking cache behavior is not changed by this rule. At delivery recompute proof from the fresh original read universe; a new competing module, even one outside preview emit scope, can invalidate proof while emitted fragment digests remain unchanged. No schema or cache-reset changes.
3. Test disabled, cold and warm fast paths at the same root with fresh packer instances; observe per-kind fact/span hits and equivalent wire/result metadata (exclude telemetry and ephemeral IDs). Then edit only an importer, edit/delete/move only a definition, and insert a competing admitted module with old source unchanged, checking changed proof/no stale edge at the actual consumer. Change budget on a warm hit without editing files; assert new decision, same useful file-local hit. Use a real local Git sibling worktree pair with a private cache root; warm one and edit the other, assert separate identity and no foreign proof or cross-worktree bytes. Exercise cleanup through pytest temp-root ownership, no destructive commands on the real checkout.

### Required Interfaces and Contracts
- No change to `ContextCache` protocol, keys for existing unchanged codecs, schema, or CLI. An incomplete or stale witness is never supplied by cache. Checking a scope cannot gain membership from hits or peer artifacts.

### Requirement Evidence
- ADR-004 and Original Request FR 5, 9; index **Scope/cache** and **Expansion**.

### Error Handling and Logging
- Existing corruption/disabled behavior remains a cache miss/bypass; no fresh proof from stale data. DEBUG per-kind hit/miss and finite invalidation cause; no query, source, or forbidden path.

### Tests
- Add actual consumer cases above; also tripwire against scans outside read filter and assert no new cache I/O with feature disabled/cache disabled.

### Acceptance Criteria
- Disabled/cold/warm packs are semantically identical for the same enabled selection policy; edits and worktree changes never return stale dependency context.

### Verification
- `uv run --locked pytest tests/context_cache/test_dependency_coverage.py tests/context_cache/test_packing.py tests/context_cache/test_facts.py tests/context_cache/test_imports.py tests/context_cache/test_peer.py`

## Task 6: Preserve workflow and CLI contracts

### Intent
Enable the pack policy in V2 preview and V2 tool stages without changing any authorization, lifecycle, prompts outside packed source, or provider contract.

### Implementation Steps
1. Enable via `context.dependency_coverage.enabled` only. Existing packed preview (`wl context build|inspect --packed`, budget) and V2 workflow composition receive policy from `Repository.config`/packer. Add no verbs, flags or implicit V2 selection. Add summary to enabled L1 JSON/human packed output and evidence to build/inspect (no text in inspect, no paths or excerpts in budget). Preserve disabled **packed** payloads and exit mapping exactly. `wl config show` naturally exposes the new effective subtree, limited to its boolean/numeric settings in both JSON and YAML. Keep `wl` and `lean_weave:main` entry wiring intact.
2. Plan remains metadata-only L0; implement resolves within the frozen, approved task scope (task targets, existing admitted one-hop and allowed matching tests), retry stays in exact attributed paths. Never traverse outside this scope even for a resolved-looking map edge; report off-scope as uncertainty without path disclosure. An in-scope dependency is never an editable target and never changes checks, tools, approval, side effects or authorizations. V1 and injected legacy providers do not invoke analysis. Preserve existing fallback rules (only the existing eligible initial implement `no_relevant_source`); dependency insufficiency, stale proof and expansion failures do not fall back.
3. In `context_authority_material`, add dependency policy and analyzer contract version only when enabled; the key must be absent when disabled so pre-existing unchanged checkpoints keep their current digest. Confirm enabled limit changes or toggles require existing nonterminal approval revalidation; terminal reads remain inert. Keep telemetry out of checkpoint/journal as a new schema; use pack-local summary and existing status/counters. Preserve order of gateway checks and reservations.
4. In `_read`, `_round` and `note_patch`, use read approval and internal forced refs but keep **only explicit read roots** as forced roots. Do not copy every `expand` fragment into `_forced`: that would turn dependency additions into depth-zero roots and erase their attribution. Add an internal staging path sharing Task 4 proof/fit: a successful enabled read records an exact proposed root and proof refs, consumes one recognized attempt, and returns metadata saying it is staged for the next guarded request. Do not mutate delivered coverage or the stage ledger yet. At `_round`, repack all roots once with actual tools, replay and revisions; recompute closure, stage the union of `(path,digest,range)` additions and unique source-body intervals, check inherited remaining limits, then commit only after `prepare_packed_request` returns a guarded request. On refusal keep the previous ledger/coverage, discard pending roots, and return before reservation/dispatch. Keep the prior owned-edit behavior for untouched roots: drop edited-path refs, re-prove affected groups, and never refund prior spent counts/bytes. Reject excess roots or an incomplete group before commit rather than trimming the list. Keep staging and ledger ephemeral.
5. Map `dependency_limit:*` from mandatory initial/sealed composition to existing `budget_exhausted` in `_refusal`, and from explicit reads to `oversized` in `_read`. Preserve `context_stale`/`stale` for invalid revisions or checked-witness failures, `context_insufficient`/`unsupported` for unmaterializable or ambiguous required targets, and existing V1 fallback allowlist. Ready uncertainty remains diagnostic, not a fabricated success for an explicit ambiguous selector.
6. Run the docs checkpoint with copyable examples of opt-in, conservative limits, `insufficient_context` vs `dependency_uncertain`, narrower rebuild and explicit expansion, frozen scope, fresh cache proofs, development-only claims and the need for a new blind holdout. Update every listed page or record why it remains unchanged. Any doc test uses an independently authored tree.

### Required Interfaces and Contracts
- Existing minimal orchestrator and `ContextProvider`/`ContextPack` remain unchanged. Only enabled V2 packed results expose new fields, without new required gateway or CLI inputs.

### Requirement Evidence
- Original Request architecture, FR 4–9, acceptance; `RULES.md`, ADR-001/002/004; index **Default**, **Enabled**, **Limits**, **Expansion**, **Scope/cache**.

### Error Handling and Logging
- Existing typed workflow preparation/error codes and CLI statuses; DEBUG counts/reasons for sealed-round refusals, expansion outcomes, and authority drift. Include no prompt text, API keys, receipts, patch data, or off-scope paths.

### Tests
- Preview L0/L1 enabled/disabled, JSON/human packed projections and JSON/YAML config; budget output not leaking paths; V1/build-only and help no-I/O; V2 implement/read/next sealed round with fake provider over an original multi-file fixture. Assert expected dependency and import intervals in the actual guarded wire. Verify two reads before one seal, forced-root bound, no depth reset, duplicate charge prevention, replay-caused refusal with ledger rollback, owned-edit invalidation without refund, stale outsider, retry and plan scope negatives, no reservation/dispatch on refusal, typed status mapping, exact fallback eligibility and authority-drift/terminal behavior. Use tracked `pyproject.toml` wiring, not a benchmark checkout.

### Acceptance Criteria
- Opt-in works on existing commands and sealed V2 stages; no default behavior, approved path, workflow transition or gateway contract changes.

### Verification
- `uv run --locked pytest tests/cli/test_context_packing.py tests/cli/test_dependency_config.py tests/workflow/test_context_lifecycle.py tests/workflow/test_context_expansion.py tests/workflow/test_orchestrator.py tests/providers/test_deepseek_packed_context.py tests/contracts`

## Phase Risks and Mitigations
- Preview scopes and productive worktree permissions differ: check actual sealed requests and independent cache namespaces.
- Adding a default policy to authority serialization would invalidate legacy resumes: omit the authority subtree unless enabled.

## Phase Completion Checklist
- Verify Tasks 5–6 independently, complete documentation checkpoint, and update `index.md`.
