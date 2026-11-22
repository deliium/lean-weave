# Phase 1: Trusted selection and scoped V2 inputs

Plan: [index.md](index.md)  
Tasks: 1–2  
Depends on: none

## Objective

Resolve context mode and reuse V2 services on the feature worktree without letting preview behavior, ranking, cache, or repository content widen scope.

## Current-Code Evidence

| Path | Symbols / contract | Why it matters |
|---|---|---|
| `src/lean_weave/app.py`, `src/lean_weave/workflow/session.py` | `create_orchestrator`, `create_context_packer`, `FeatureSession._authority_digest` | Worktree root is explicit; V2 preview configuration is not workflow policy. |
| `src/lean_weave/context/provider.py` | `DeterministicContextProvider._targeted_index`, `_implement`, `_retry`, `build` | Existing source bounds and build-only injection compatibility; retry currently computes unused import scope. |
| `src/lean_weave/context/repository.py`, `src/lean_weave/context/retriever.py`, `src/lean_weave/context/packing_models.py` | `scan_retrieval_sources`, `_execute`, `PackRequest.output_paths` | Preview opens the whole admitted corpus before output selection. |
| `src/lean_weave/executor/{approval,workspace}.py` | `ToolApproval`, `Workspace._name_policy`, `Workspace.authorize` | Executor pathspec read policy is separate from repository admission; a write candidate or resolved import is not itself read approval. |
| `src/lean_weave/code_index/index.py`, `src/lean_weave/repository_map/service.py` | `scoped_facts`, `fact_loader`, `graph_for_retrieval_scan` | Full-generation coverage and scoped live fact reuse are distinct. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `src/lean_weave/config/models.py`, `src/lean_weave/app.py`, `src/lean_weave/workflow/{session,lifecycle,orchestrator}.py` | Modify | Default-preserving selection, lazy root-bound construction and versioned V2 authority input. |
| `src/lean_weave/workflow/context_lifecycle.py` | Create | Private workflow adapter for trusted stage projection, scope, request and local counters; not a second driver. |
| `src/lean_weave/context/{repository,retriever,packer,packing_source}.py`, `src/lean_weave/repository_map/service.py` | Modify | Scoped scan, ranking/packing hooks and read-only scoped map evidence. |
| `src/lean_weave/executor/workspace.py` | Modify | Share executor read-pattern decision semantics with the scoped scanner without opening another source snapshot. |
| `tests/config/test_context_config.py`, `tests/workflow/test_session.py`, `tests/context/test_repository.py`, `tests/context/test_retriever.py` | Modify | Settings, drift, source-open and cache/graph scope tests. |

## Task 1: Bind context selection to workflow authority

### Intent
Make selection explicit and leave existing installations and injected `ContextProvider.build` implementations compatible.

### Implementation Steps
1. Add strict `ContextConfig.workflow_mode: Literal["v1", "v2", "v2-with-v1-fallback"] = "v1"` in `config/models.py`. This is product config, independent of preview `packing.cli_default` and cache `enabled`. No new CLI flag. Public start/nonterminal resume use the existing external trusted operator config.
2. In `create_orchestrator`, instantiate the private adapter only for a selected V2 mode with the resolved `workspace_root`, effective read policy, and exact existing `PlanPolicy`/`ToolApproval` inputs. Construct services lazily; V1, help, verify, status/result/archive/cancel and terminal resume do not open new stores. Injected orchestrator and injected build-only ContextProvider retain precedence; do not attempt to call preview methods on injected providers. An explicit injected provider selects the existing V1 path as dependency-injection behavior, not an automatic runtime fallback.
3. In `FeatureSession._authority_digest` and `workflow.lifecycle.authority_digest`, extend the existing digest with V2-only versioned material covering effective mode, protected/excluded paths and context admission limits, the frozen `ToolApproval` read allowlist, exclusions and source/tool limits, plan call/token caps, task/feature and tool-loop limits, packing/retrieval/cache settings, and effective model/thinking/output cap. Use canonical safe material with a V2 discriminator, excluding secrets; leave legacy digest input byte-for-byte unchanged. Both start and resume must compute the same effective material as the factory rather than independently interpreting V2 config. Reject drift using the existing authority comparison before another context dispatch; do not modify `FeatureState`, `ControlRecord` shape, `StateStore`, or checkpoint versions.
4. On V2 start, supply `_plan_stage` a live worktree baseline callback rather than the captured `lambda: baseline`; use the existing before/after comparison, sampled before selecting planning context and after the plan response. If bytes changed during planning, fail `baseline_drift` without publishing a plan/review gate or running implementation. Keep the live resume fingerprint and legacy V1 behavior. A live baseline comparison does not replace the scoped scan's own freshness check.

### Required Interfaces and Contracts
- Existing `ContextProvider.build(stage, state)`, provider injection and `WorkflowOrchestrator` API remain unchanged. Optional helper sits behind the concrete driver constructor; default absent.
- V2 selection alone cannot authorize a write, check or source path. Frozen context mode changes require a new feature, not silent policy changes on resume.

### Requirement Evidence
Original Request §§1, 3, 8–9; ADR-004; reconciliation rows V1/injection and mode/review drift.

### Error Handling and Logging
Strict YAML rejects invalid mode. Authority change uses existing typed drift refusal. DEBUG effective mode and injection precedence; WARN drift category, never config dump, approval token or key.

### Tests
Config defaults, valid values, invalid strings/types, preview flags independent; session tests for legacy authority fixture equality, V2 mode/read-policy and plan-cap drift before context preparation or a provider call, and an approved-file change during the scripted planning response (no gate or implementation despite successful PLAN transport). Cover terminal no-I/O and build-only injection.

### Acceptance Criteria
Old configs and saved V1 runs behave unchanged; V2 can be selected independently and cannot be swapped on a live tracked run.

### Verification
`uv run --locked pytest tests/config/test_context_config.py tests/workflow/test_session.py tests/context/test_retrieval_provider.py` — all pass.

## Task 2: Build a scoped scan and map path

### Intent
Use all five V2 components while preserving the actual V1 source-read boundary and the V2 preview contract.

### Implementation Steps
1. Add a separate internal scoped scan input to `Repository.scan_retrieval_sources` and thread it through retriever ranking, packing and session rematerialization. Freeze the explicit normalized paths in the strict scan policy; `None` is preview-only and empty means no source. Filter at the strict scanner **before source open**, retain bounded inventory/path metadata, nested-ignore/control witnesses and no-follow policy. Extend an admitted snapshot with an optional private raw-byte revision from the same read (executor-compatible SHA256), distinct from the canonical content digest; keep preview/public serialized shapes unchanged.
2. Scope seeds: plan uses admitted caller-approved paths for interface analysis only; implement uses active task files, uniquely resolved static one-hop imports and (when `plan.settings.testing is True`) existing `matching_tests` candidates; retry reads/emits only strictly attributed active-task paths from Task 3. Implement dependency admission using V1 `_targeted_index` semantics over the strict snapshots and bounded eligible-name inventory: resolve statically from seed files, add those direct targets/tests once, never follow them transitively. Reuse the same scan's admitted seed snapshots when adding paths, then freeze the final scope and witness for the operation. If a path lacks proof, omit optional evidence or refuse required evidence; never fall back to a broader scan. Only task files remain candidates for effectful tools.
   - Intersect each seed or discovered dependency/test with frozen executor read policy **before** opening a body. Share the executor's pure pathspec matching semantics (including no-pattern behavior); combine its exclusions/protected paths and the stricter effective byte limit with strict descriptor-relative ignore/no-follow admission. Do not run `Workspace.read` as a second pass. Index/cache hit, approved write target, same-package import, and fallback are not exceptions. Omit optional or planning-map entries denied by either policy; refuse a required implement/retry source rather than treating denial as no-anchor fallback.
3. Use `CodeIndex.fact_loader`/`scoped_facts` only for already admitted current snapshots; on miss use existing AST extraction. Add an additive **scoped** `RepositoryMap` projection from checked facts/live facts using existing graph and metadata map renderers, with no inference of missing off-scope edges. Never pass a partial scan to `read_generation_for_retrieval_scan` as a complete checkout. Connect `ContextRetriever` ranking and `ContextPacker` selection to this same scan and scoped graph; disable Git recency/diff signals in workflow. Planning outbound maps are L0 metadata (paths, names, signatures, known direct dependencies only), never source bodies; a stable empty map yields a metadata-only plan, not a whole-checkout preview.
4. Open optional `ContextCache` lazily on the feature worktree namespace. Reuse live-proof per-file facts/spans and relation/map metadata where proofs match. Keep preview and scoped keys disjoint; do not enable scoped rank/pack persistence unless key and witness capture exact mode/read/output scope, query fingerprint, versions and effective configuration. For this rollout, bypass shared ranking/header persistence for scoped scans, retaining checked per-file reuse. No implicit index maintenance, peer reuse or origin cache reads.

### Required Interfaces and Contracts
- New internal scoped scan/rank hooks take trusted scope explicitly; `PackRequest.output_paths` remains an emission filter. The scoped scan retains existing read budgets, secret checks and freshness semantics. No new store or provider API.
- Checked-index miss/corruption or cache absence only degrades optimization. Preview/index generation and cache maintenance stay unchanged.

### Requirement Evidence
Original Request §§1–2, 4–7, 9; ADR-004; v2-01 checked reads, v2-02 typed map, v2-03 strict scan, v2-04 emission scope, v2-05 checkout-local proof.

### Error Handling and Logging
DEBUG stage, admitted/omitted counts, graph/fact origin and cache deltas. WARN stale/incomplete required proof via static reason codes. No source bodies, task strings, raw denied paths, or cache keys in logs.

### Tests
Instrument actual descriptor-relative source opens in `test_repository.py` and `test_retriever.py`: empty and scoped sets, symlinks/nested ignore/secret/protected paths, unrelated sibling never opened, a uniquely resolved import and matching test forbidden by `read_allow`, executor-only exclusion of a required seed, one-hop only, raw versus canonical CRLF revision, late witness invalidation, partial graph never reused as a generation. Cover absent/stale/healthy index and disabled/cold/warm cache with identical outbound selection; retain preview regressions.

### Acceptance Criteria
Reader and outbound scopes are independently enforced; V2 planning has a bounded source-free map and no unrelated body reads; index and cache never authorize selection.

### Verification
`uv run --locked pytest tests/context/test_repository.py tests/context/test_retriever.py tests/code_index/ tests/repository_map/ tests/context_cache/` — existing and scoped cases pass.

## Phase Risks and Mitigations
- Scoped completeness can be mistaken for full-generation completeness; use a distinct scope witness and regression tripwire.
- Canonical digest is unsafe as a patch precondition; hash original bytes at admission and retain the existing executor precondition.

## Phase Completion Checklist
- Tasks 1–2 meet their acceptance criteria and verification passes.
- Update only `index.md` for task progress after verification.
