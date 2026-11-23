# Phase 1: Independent development baselines

Plan: [index.md](index.md)  
Tasks: 1  
Depends on: none

## Objective

Establish a repeatable, offline comparison before changing product selection.

## Current-Code Evidence

| Path | Symbols | Why it matters |
|---|---|---|
| `src/lean_weave/context/models.py` | `ContextQuery`, `ContextPack.excerpts` | V1 can be scored by actual source ranges, not symbols or path mentions. |
| `src/lean_weave/context/provider.py` | `DeterministicContextProvider._implement`, `build` | V1 implement uses `ExpansionLevel.RANGES`, targeted index/scope, and structured plan digests; a bare builder request would not reproduce it. |
| `src/lean_weave/context/packer.py` | `ContextPacker.build`, `open_task`, `expand` | The existing V2 arm must use real packed source and statuses. |
| `tests/repository_map/test_retrieval_graph.py` | `_write_tree`, `test_retrieval_graph_keeps_circular_resolved_edges` | Ordinary `tmp_path` and checkout-local index patterns; retain as compatibility evidence, not scoring examples. |
| `tests/conftest.py` | `offline_network_guard` | Guards in-process network only; avoid CLI subprocesses except local Git isolation tests. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `tests/retrieval_dependencies/__init__.py` | create | Package for independent development suite. |
| `tests/retrieval_dependencies/dev_cases.py` | create | Original source trees and frozen case metadata. |
| `tests/retrieval_dependencies/dev_eval.py` | create | Standalone development-only scorecard, not registered with the historical benchmark. |
| `tests/retrieval_dependencies/test_dev_eval.py` | create | Metric and baseline contract tests. |
| `tests/retrieval_dependencies/dev_baseline.json` | create | Frozen non-timing V1 and V2 measurements, with exact fixture/profile identity. |

## Task 1: Freeze fixtures and baseline adapters

### Intent
Make label/fixture isolation and comparable coverage measurable without opening or running the known holdout.

### Implementation Steps
1. Author original in-code trees from generic mechanisms: (a) a local imported class used in a call and annotation; (b) aliased `from` and dotted module imports with both used and unused imports; (c) relative package imports with a two-hop chain; (d) same-file helper plus shadowing in a nested scope; (e) a circular dependency; (f) duplicate module/definition, wildcard and dynamic lookups as negative controls; (g) a large proven requirement exceeding each independent budget; (h) a small no-dependency control and an optional, explicitly expandable target. Invent names and layouts; include filler long enough that not every definition can fit. Keep score cases distinct from implementation unit cases.
2. Freeze case IDs, neutral task queries, mandatory anchor intervals, must-cover definition **and** import intervals, negative intervals, expansion script, budget profile, and classification (`coverage`, `uncertainty`, `budget`, `control`) as source-controlled metadata before product changes. Labels and selector scripts are evaluator-only inputs; never pass expected dependency intervals, names, paths, or negative expectations into product ranking. Use an identical task query and equivalent declared edit target per arm; keep a separate explicit-selector contract test out of aggregate recall. Record V1's native stage mode/caps and V2's native pack caps, plus common counted-budget eligibility; document representational differences, do not silently truncate baseline output.
3. For V1 construct a minimal valid `FeatureState` with one active implementation task and call the public `DeterministicContextProvider.build("implement", state)`. Capture `bundle["pack"].excerpts` (primary emitted provenance only) and `bundle["messages"]` from the same real `RANGES` build. For V2 use production `ContextPacker.open_task(PackRequest(required_paths=(edit_target,), ...))` with the same neutral query and dependency coverage absent/disabled; explicitly build a `CodeIndex` under the private cache root before measurement and supply `RepositoryMap.graph_for_retrieval_scan`. This is a packer comparison against the V1 implement reference, not a comparison of sealed workflow scope or billing. Record the actual scope of each arm; keep index availability, scope, request, and Git signal policy identical between V2 and candidate. Task 6 separately exercises sealed-workflow delivery. Record the pre-change revision and persist fixture hash, profile, arm and normalized results in `dev_baseline.json`; exclude machine timings and absolute paths. The planning reference revision is `91fc0724e5eecc6af0fa519c50702b977b4bdaf9`; if implementation starts at a different revision, record the actual untouched baseline and why.
4. In the scorecard use one `ConservativeByteCounter.count_request` on a common canonical envelope for emitted messages (same model, output reserve, tools absent), also retain native estimator and input/output where available. Count coverage only when source ranges deliver the whole expected interval at the expected revision; union valid ranges from **delivered** expansions, not map rows, refs, planned/proposed units or refused packs. Record initial and final recall and missing counts (micro-average over coverage cases, per case as well), anchor coverage, unexpected/forbidden emissions, input/total tokens, dependency-attributed tokens, actual expansion frequency (`cases with a successful explicit expansion / cases eligible`), attempts/refusals, and latency. Baseline dependency attribution uses the labels offline against already-rendered units; charge each intersecting emitted unit once, including its header, and label this separately from candidate-native charges. In unready arms record zero delivered tokens/coverage and the refusal separately; never count refusal as savings. Compare attempted/delivered totals, and measure latency from just before product build through return, excluding fixture setup and evaluation scoring; report median and p95 over 21 repeats after one warm-up, cold and warm cache separately, with environment/estimator fields. V1 has no comparable L2 API: mark expansion not applicable rather than manufacture a fallback.
5. Expose `uv run --locked python -m tests.retrieval_dependencies.dev_eval --arms v1,v2 --output <path>` and later `--arms v1,v2,candidate`; the runner accepts no external corpus or labels; reject unknown arms and existing output paths. Use a fixed label-free L2 policy: among registered candidates, try the first exact not-yet-covered symbol sorted by `(path, qualified_name, start_line, end_line)`, repeat only while attempts remain and the previous delivery succeeded; cases opt into the script before baseline freeze. Keep revision, fixture hash, profiles and non-timing payload comparable. After freeze, any needed correction is documented and reruns all arms against the same new manifest, not candidate-only.

### Required Interfaces and Contracts
- `DevCase` (frozen dataclass): source mapping, neutral task and edit target, inclusive evidence intervals, profile and optional typed L2 script. `ArmRun` records actual admitted intervals, readiness, budget/cost measurements and expansion counts; use normalized stable refs, no random receipts, timestamps or checkout paths in deterministic comparison.
- Use fresh temporary roots and external cache directories per case/profile; no existing production benchmark fixtures, code execution from fixture sources or network.

### Requirement Evidence
- Original Request evaluation discipline and FR 10–12; index combinations **Evaluation** and **Default**.

### Error Handling and Logging
- Fail closed on empty fixture identity, invalid intervals, arm/manifest mismatch, missing readiness, or missing comparability fields; never replace unavailable metrics with zero. DEBUG logs: aggregate case ID, arm, eligibility and reason; no source, raw query or secrets.

### Tests
- Metric tests for adjacent vs gapped ranges, wrong digest, duplicate charging, blocked/undelivered fragments, and map-only hits. Exercise a real temp checkout in each baseline arm and require stable normalized results across repeated runs. Assert the V1 capture comes from the public implement bundle and that both V2 arms receive the same ordinary task target, never expected dependency refs.

### Acceptance Criteria
- Independent corpus frozen, legacy baselines executable and analyzable without known holdout inputs; the protected artifacts remain untouched.

### Verification
- `uv run --locked pytest tests/retrieval_dependencies/test_dev_eval.py`
- `uv run --locked python -m tests.retrieval_dependencies.dev_eval --arms v1,v2 --output /tmp/opencode/v2-09-dev-baseline.json` — both arms produce all required metrics.

## Phase Risks and Mitigations
- Different legacy representations could inflate one arm: score exact delivered intervals with one wire estimator and report native limits separately.

## Phase Completion Checklist
- Verify Task 1 and record the frozen baseline before any ranking/packing change. Update its checkbox in `index.md`.
