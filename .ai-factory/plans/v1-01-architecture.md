# Implementation Plan: Architecture and Technical Validation

Branch: none
Created: 2026-11-08

## Original Request

Plan ID: v1-01-architecture
Plan Title: Architecture and Technical Validation

Goal:
Design and validate the architecture of a lightweight, token-efficient AI coding pipeline.

Project context:
The system will automate feature development using DeepSeek API, LangGraph, reusable AI Factory instructions, local coding tools, and structured workflow state.

The primary objective is to minimize total LLM token consumption while preserving implementation quality.

Requirements:
1. Analyze the current repository and available AI Factory instructions.
2. Investigate reuse of aif-plan and aif-implement methodologies without launching nested autonomous agent sessions.
3. Validate LangGraph StateGraph, checkpointing, interruption, and resume capabilities.
4. Validate DeepSeek API compatibility, tool calling, structured output, reasoning modes, and token usage reporting.
5. Compare LangGraph with a minimal Python state machine. Prefer LangGraph only if its benefits justify its complexity.
6. Define component boundaries and interfaces:
   - CLI
   - LLMProvider
   - ContextProvider
   - WorkflowOrchestrator
   - CodeExecutor
   - VerificationRunner
   - StateStore
   - MetricsCollector
7. Define FeatureState, FeaturePlan, ImplementationTask, and verification result schemas.
8. Define context isolation between planning, implementation, and retry stages.
9. Define security and Git isolation requirements.
10. Identify technical risks and validate uncertain assumptions.

Constraints:
- No MCP, ACP, Docker, background workers, or multi-agent architecture in MVP 1.
- Avoid unnecessary dependencies and abstraction layers.
- Keep state structured and independent of full chat history.
- Prefer deterministic operations over LLM calls.
- Validate external API assumptions against current documentation.

Deliverables:
- Architecture document.
- Architecture Decision Records.
- Component contracts.
- LangGraph state and transition design.
- Proposed Python project structure.
- Technical risk register.
- MVP 1 acceptance criteria.

Do not implement production functionality.
Small technical spikes are allowed when necessary.

Save the plan using the exact identifier:
v1-01-architecture

## Settings
- Testing: yes
- Logging: verbose
- Docs: yes

## Requirements Reconciliation

Authority: User request for this plan > current DeepSeek/LangGraph documentation (for external API/runtime assumptions) > installed AI Factory skill texts under `.cursor/skills/` (methodology extraction only) > greenfield repository facts. No DESCRIPTION/ARCHITECTURE/ROADMAP/RESEARCH yet (DESCRIPTION is produced by this plan).

| Decision / supported combination | Source path and section | Verification evidence |
|----------------------------------|-------------------------|-----------------------|
| Architecture/validation only; no production feature pipeline code | User request — Deliverables / "Do not implement production functionality" | Plan tasks produce docs/ADRs/contracts/spikes only; no CLI product commands beyond spikes |
| Spikes allowed when needed to validate uncertain assumptions | User request — Constraints / Deliverables | Tasks 5–7 include bounded spike scripts under `spikes/` |
| No MCP, ACP, Docker, background workers, multi-agent in MVP 1 | User request — Constraints | Architecture + ADR forbid these; risk register lists deferred items |
| Prefer deterministic ops over LLM; state ≠ full chat history | User request — Constraints | Schemas/contracts require structured FeatureState; MetricsCollector tracks token use |
| Prefer LangGraph only if benefits justify complexity | User request — Requirement 5 | ADR-002 records comparison result with go/no-go criteria |
| Reuse aif-plan / aif-implement methodology without nested agent sessions | User request — Requirement 2 | Methodology note extracts workflow steps as library/prompts, not Task/subagent launches |
| Plan prefs default to tests+verbose+docs for future plans | User reply + `.ai-factory/config.yaml` workflow.plan_* | Config + skill-context present; this plan Settings match |
| DeepSeek live vs offline probe | User request — Requirement 4; Task 5 | Live when `DEEPSEEK_API_KEY` set; offline fixture assertions otherwise; both documented |
| Thinking on/off × tools present/absent (`reasoning_content` echo rules) | DeepSeek thinking-mode docs; Task 5 | Validation matrix + tests cover required combinations |
| Git absent → StateStore under `.lean-weave/` only; git present → branch/worktree rules | User request — Requirement 9; current repo has no `.git` | Task 10 isolation doc + MVP acceptance criteria exercise both combinations |
| LangGraph transition design always published (adopted or deferred alternative) | User request — Deliverables | Task 11 structure doc includes LangGraph design section regardless of ADR-002 choice |

## Commit Plan
- **Commit 1** (after tasks 1–4): `docs: inventory repo, spike tooling, and AI Factory methodology`
- **Commit 2** (after tasks 5–7): `test: add DeepSeek and orchestrator spikes with decision evidence`
- **Commit 3** (after tasks 8–10): `docs: define component contracts, schemas, and isolation rules`
- **Commit 4** (after tasks 11–14): `docs: publish architecture, ADRs, risks, and MVP 1 acceptance criteria`

## Tasks

### Phase 1: Repository and methodology baseline

- [x] Task 1: Inventory repository and AI Factory instruction surface
  Catalog the greenfield layout (`pyproject.toml`, `src/lean_weave/`, `src/main.py` PyCharm sample, `.cursor/skills/`, `.opencode/skills/`, `.ai-factory/`). Summarize which aif-* skills matter for a coding pipeline (especially `aif-plan`, `aif-implement`, `aif-verify`, `aif-commit`, `aif-warmup`) vs deferred. Note the entrypoint tension between `src/main.py` and `lean_weave:main` for Task 11. Write `.ai-factory/references/v1-01-repo-inventory.md` with: stack facts, missing AI Factory core artifacts, skill inventory table, and token-relevant observations (skill docs are large; pipeline must not load them wholesale).

  LOGGING REQUIREMENTS:
  - INFO: inventory start/complete with file counts by area
  - DEBUG: skipped paths and why (e.g. `.venv`, binary)
  - ERROR: unreadable required paths
  - Format: `[Inventory.run] message {path_count=...}`
  - Control via `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`)

  Files: `.ai-factory/references/v1-01-repo-inventory.md`

- [x] Task 2: Bootstrap spike tooling and repo hygiene
  Update `pyproject.toml` so spikes and schema tests are runnable with uv: add dependency groups for runtime/spike libs (`openai`, `langgraph`, `langgraph-checkpoint-sqlite`, `pydantic`) and dev (`pytest`, typing extras as needed). Add minimal pytest config if required. Add `.gitignore` covering `.venv/`, `__pycache__/`, `.lean-weave/`, spike checkpoint DBs, `.pytest_cache/`, and secrets patterns. Document canonical commands (`uv sync`, `uv run pytest …`) in the inventory note or a short `spikes/README.md`. Do not implement the production CLI.

  LOGGING REQUIREMENTS:
  - INFO: dependency groups written; ignore rules count
  - DEBUG: packages pinned or left unpinned and why
  - ERROR: `uv sync` / dry-run failures
  - Format: `[SpikeBootstrap.write] message {group=..., ok=...}`
  - Control via `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`)

  Files: `pyproject.toml`, `.gitignore`, optional `spikes/README.md`, update `.ai-factory/references/v1-01-repo-inventory.md` if commands live there
  Depends on: 1

- [x] Task 3: Extract reusable aif-plan / aif-implement / aif-verify methodology without nested agents
  Read `.cursor/skills/aif-plan/SKILL.md` (+ TASK-FORMAT), `.cursor/skills/aif-implement/SKILL.md` (+ IMPLEMENTATION-GUIDE), and a thin pass over `.cursor/skills/aif-verify/SKILL.md` for VerificationRunner mapping. Produce a methodology map that separates: (a) durable artifacts (plan markdown, checkboxes, settings, commit checkpoints, verify gates), (b) agent-runtime behaviors that must NOT be reused (Task/Explore subagents, AskUserQuestion loops, MCP/Handoff, nested `/aif-*` invocations including nested `/aif-verify`), (c) prompt/procedure fragments worth reusing as local templates / deterministic checks. Write `.ai-factory/references/v1-01-aif-methodology-reuse.md` with an explicit "no nested autonomous sessions" rule and a proposed lean-weave mapping (plan stage → implement stage → verify stage) driven by WorkflowOrchestrator, not by spawning agents. For verify: list which gates become deterministic VerificationRunner checks vs what remains human/out-of-band.

  LOGGING REQUIREMENTS:
  - INFO: skills parsed and section counts extracted
  - DEBUG: each rejected nested-agent pattern with source anchor
  - WARN: ambiguous skill steps that need human decision in ADR later
  - Format: `[MethodologyExtract.run] message {skill=..., pattern=...}`
  - Control via `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`)

  Files: `.ai-factory/references/v1-01-aif-methodology-reuse.md`
  Depends on: 1

- [x] Task 4: Define token-efficiency principles for context loading
  From methodology + inventory, define rules for ContextProvider: stage-scoped prompts, artifact digests vs full files, deterministic file selection, max context budgets, and what never enters LLM context (secrets, full skill trees, entire chat history). Capture as a short design note in `.ai-factory/references/v1-01-token-budget-principles.md` that later feeds ARCHITECTURE.md and ContextProvider contract.

  LOGGING REQUIREMENTS:
  - INFO: budgets/principles finalized
  - DEBUG: candidate context classes and include/exclude decisions
  - Format: `[TokenPrinciples.define] message {budget=...}`
  - Control via `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`)

  Files: `.ai-factory/references/v1-01-token-budget-principles.md`
  Depends on: 1, 3
<!-- Commit checkpoint: tasks 1-4 -->

### Phase 2: External capability spikes and orchestrator decision

- [x] Task 5: DeepSeek API validation spike (docs + optional live probe)
  Against current DeepSeek docs (tool calls, thinking mode, JSON/`strict` schema, usage fields), write spike `spikes/deepseek_probe/` that:
  - Documents supported matrix: tool calling (thinking on/off), `response_format` json_object caveats, beta `strict` tool schemas, `reasoning_content` echo rules when `tools` present, usage/`reasoning_tokens` / cache hit fields.
  - Provides a small script using OpenAI-compatible client (`base_url=https://api.deepseek.com`) that, when `DEEPSEEK_API_KEY` is set, exercises: non-thinking tool call, thinking+tools with reasoning_content round-trip, structured JSON output, and prints usage metrics; when key absent, runs offline assertion tests against fixture response shapes.
  - Records findings in `.ai-factory/references/v1-01-deepseek-validation.md` including any doc/API mismatches and MVP implications for LLMProvider.

  LOGGING REQUIREMENTS:
  - INFO: mode (live/offline), cases run, pass/fail
  - DEBUG: request metadata without secrets (model, thinking flag, tool count); never log API keys or full prompts if they contain secrets
  - INFO/DEBUG: token usage fields when present (`prompt`, `completion`, `reasoning_tokens`, cache hits)
  - ERROR: HTTP/API failures with status and safe error body excerpt
  - Format: `[DeepSeekProbe.case] message {case=..., ok=...}`
  - Control via `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`)

  Files: `spikes/deepseek_probe/`, `.ai-factory/references/v1-01-deepseek-validation.md`, tests under `tests/spikes/test_deepseek_probe.py`
  Depends on: 2, 4

- [x] Task 6: LangGraph checkpoint / interrupt / resume spike
  Implement `spikes/langgraph_probe/` demonstrating StateGraph with checkpointers, `interrupt()`, and `Command(resume=...)` using a stable `thread_id`. Cover: pause+resume, continue-after-error pattern (`invoke(None, config)`), and what state is persisted vs re-executed (node restart semantics). Use `InMemorySaver` for fast unit tests. Also exercise a durable path with `SqliteSaver` (`langgraph-checkpoint-sqlite`) including process restart then resume from the same sqlite file + `thread_id` (proves requirement 3 beyond in-process memory). Document production checkpointer options without adding Postgres/Redis deps. Capture results in `.ai-factory/references/v1-01-langgraph-validation.md`.

  LOGGING REQUIREMENTS:
  - INFO: graph compile, interrupt hit, resume complete, thread_id, checkpointer kind
  - DEBUG: checkpoint ids / next nodes (metadata only)
  - ERROR: resume mismatches or missing checkpointer
  - Format: `[LangGraphProbe.step] message {thread_id=..., event=...}`
  - Control via `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`)

  Files: `spikes/langgraph_probe/`, `.ai-factory/references/v1-01-langgraph-validation.md`, `tests/spikes/test_langgraph_probe.py`
  Depends on: 2, 4

- [x] Task 7: Compare LangGraph vs minimal Python state machine; record ADR-002 draft evidence
  Build a minimal deterministic state-machine spike (`spikes/minimal_sm_probe/`) that can model the same plan→implement→verify→retry transitions (from Task 3 methodology map) with explicit persisted FeatureState JSON. Compare against LangGraph on: dependency weight, interrupt/resume ergonomics (including durable resume evidence from Task 6), observability, testability, token-irrelevant complexity, and fit for MVP 1 constraints (no workers/multi-agent). Write comparison table and recommendation (adopt LangGraph / defer / hybrid) into `.ai-factory/references/v1-01-orchestrator-comparison.md`. Do not finalize production code—decision is an architecture input for Task 13 ADRs.

  LOGGING REQUIREMENTS:
  - INFO: comparison dimensions evaluated and recommended option
  - DEBUG: LOC/deps estimates and scenario timings if measured
  - Format: `[OrchestratorCompare.evaluate] message {option=..., score=...}`
  - Control via `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`)

  Files: `spikes/minimal_sm_probe/`, `.ai-factory/references/v1-01-orchestrator-comparison.md`, `tests/spikes/test_minimal_sm_probe.py`
  Depends on: 3, 6
<!-- Commit checkpoint: tasks 5-7 -->

### Phase 3: Contracts, schemas, and isolation

- [x] Task 8: Define component boundaries and interface contracts
  Specify contracts for CLI, LLMProvider, ContextProvider, WorkflowOrchestrator, CodeExecutor, VerificationRunner, StateStore, MetricsCollector: responsibility, inputs/outputs, forbidden dependencies, sync vs async stance, error model, and logging expectations. LLMProvider must incorporate Task 5 DeepSeek findings (thinking/tools/`reasoning_content`/usage fields). Store as `.ai-factory/references/v1-01-component-contracts.md` and required Protocol-only modules under `src/lean_weave/contracts/` (typing only; no runtime behavior). Add import/typing tests under `tests/contracts/`. Enforce MVP exclusions: no MCP/ACP clients, no Docker orchestration, no background workers, no multi-agent routers.

  LOGGING REQUIREMENTS:
  - INFO: each contract finalized with name and dependency edges
  - DEBUG: rejected dependency edges with reason
  - Format: `[Contracts.define] message {component=...}`
  - Control via `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`)

  Files: `.ai-factory/references/v1-01-component-contracts.md`, `src/lean_weave/contracts/*.py`, `tests/contracts/`
  Depends on: 3, 5, 7

- [x] Task 9: Define FeatureState, FeaturePlan, ImplementationTask, and verification result schemas
  Produce JSON Schema and/or Pydantic models for FeatureState, FeaturePlan, ImplementationTask, VerificationResult (and any minimal supporting enums). Requirements: serializable, chat-history-independent, stage-aware, sufficient for interrupt/resume and retries, include token/metrics hooks (ids/counters only). Document field semantics and state transitions in `.ai-factory/references/v1-01-state-schemas.md`. If LangGraph is recommended, map schema fields to graph state + reducers; if minimal SM is recommended, map to explicit transition table.

  LOGGING REQUIREMENTS:
  - INFO: schema versions and entity list
  - DEBUG: transition table edges
  - ERROR: schema validation failures in unit tests
  - Format: `[Schemas.define] message {entity=..., version=...}`
  - Control via `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`)

  Files: `.ai-factory/references/v1-01-state-schemas.md`, `src/lean_weave/schemas/` (models + tests in `tests/schemas/`)
  Depends on: 7, 8

- [x] Task 10: Define context isolation, security, and Git isolation requirements
  Write `.ai-factory/references/v1-01-isolation-security.md` covering:
  - Context isolation between planning / implementation / retry (what ContextProvider may load per stage; hard bans on leaking other-stage transcripts).
  - Secret handling (env-only API keys; never persist prompts containing secrets; redacted logs).
  - CodeExecutor sandbox expectations for MVP 1 (local subprocess, cwd limits, no network from tests unless opted in).
  - Git isolation combinations: (a) git absent — StateStore only under `.lean-weave/`, no git ops; (b) git present — worktree/branch policy, forbid destructive git ops in MVP, no push by default. Both combinations must appear in MVP acceptance criteria later.
  - Threat notes for prompt injection via repo files and mitigation via allowlists.

  LOGGING REQUIREMENTS:
  - INFO: isolation rules published
  - WARN: residual risks deferred to risk register
  - Format: `[Isolation.define] message {area=...}`
  - Control via `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`)

  Files: `.ai-factory/references/v1-01-isolation-security.md`
  Depends on: 4, 8, 9
<!-- Commit checkpoint: tasks 8-10 -->

### Phase 4: Architecture publication and MVP gate

- [x] Task 11: Propose Python project structure and LangGraph/SM transition design
  Publish proposed package layout under `src/lean_weave/` (cli, providers, context, orchestrator, executor, verify, state, metrics, schemas, contracts) plus `spikes/`, `tests/`, `.lean-weave/` runtime state dir, and AI Factory artifact locations. Explicitly resolve `src/main.py` vs `lean_weave:main` (keep/remove/rehome) with rationale. Include a state/transition diagram (mermaid or tables) for the chosen orchestrator approach **and** a LangGraph state/transition design section even if ADR-002 defers LangGraph (document as deferred alternative so the deliverable remains satisfied). Save as `.ai-factory/references/v1-01-project-structure.md`.

  LOGGING REQUIREMENTS:
  - INFO: structure published; module count; main.py decision
  - Format: `[Structure.propose] message {modules=...}`
  - Control via `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`)

  Files: `.ai-factory/references/v1-01-project-structure.md`
  Depends on: 8, 9, 10

- [x] Task 12: Author minimal project DESCRIPTION
  Write `.ai-factory/DESCRIPTION.md` capturing purpose (token-efficient AI coding pipeline), stack (Python ≥3.12, uv, DeepSeek, chosen orchestrator direction), MVP 1 exclusions (no MCP/ACP/Docker/workers/multi-agent), and pointers to ARCHITECTURE.md / this plan. Keep it short; no speculative product roadmap. Align wording with Task 11 structure decisions.

  LOGGING REQUIREMENTS:
  - INFO: DESCRIPTION written with stack tags
  - Format: `[Description.write] message {path=...}`
  - Control via `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`)

  Files: `.ai-factory/DESCRIPTION.md`
  Depends on: 11

- [x] Task 13: Write ARCHITECTURE.md, ADRs, risk register, and MVP 1 acceptance criteria
  Create:
  - `.ai-factory/ARCHITECTURE.md` — system overview, boundaries, token strategy, orchestrator decision summary, MVP exclusions.
  - `.ai-factory/adr/ADR-001-component-boundaries.md`
  - `.ai-factory/adr/ADR-002-orchestrator-langgraph-vs-minimal.md`
  - `.ai-factory/adr/ADR-003-llm-provider-deepseek.md`
  - `.ai-factory/adr/ADR-004-context-isolation-and-state.md`
  - `.ai-factory/references/v1-01-risk-register.md` — technical risks, assumption status (validated/open), mitigation, owner for later plans.
  - `.ai-factory/references/v1-01-mvp1-acceptance-criteria.md` — testable acceptance checklist for MVP 1 architecture gate (not product feature gate), including git-absent vs git-present isolation checks and spike evidence paths.
  Update README with a short project blurb pointing to architecture docs and DESCRIPTION. Do not implement the production pipeline.

  LOGGING REQUIREMENTS:
  - INFO: each artifact written
  - WARN: open risks still unvalidated
  - Format: `[ArchitecturePublish.write] message {artifact=...}`
  - Control via `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`)

  Files: `.ai-factory/ARCHITECTURE.md`, `.ai-factory/adr/*.md`, `.ai-factory/references/v1-01-risk-register.md`, `.ai-factory/references/v1-01-mvp1-acceptance-criteria.md`, `README.md`
  Depends on: 5, 7, 9, 10, 11, 12

- [x] Task 14: Wire docs checkpoint artifacts for /aif-docs and verify architecture gate
  Ensure docs policy is satisfied: add `docs/architecture.md` (or link page) summarizing the architecture for humans, link from README docs table, and run the test suite (`pytest tests/spikes tests/schemas tests/contracts`) documenting results in the MVP acceptance criteria file. Confirm no production orchestrator CLI was introduced beyond spikes. Mark plan complete only when acceptance criteria checklist is filled with evidence paths.

  LOGGING REQUIREMENTS:
  - INFO: docs page created; pytest summary (passed/failed/skipped)
  - ERROR: failing tests block completion
  - Format: `[DocsGate.complete] message {tests_passed=..., docs=...}`
  - Control via `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`)

  Files: `docs/architecture.md`, `README.md`, `.ai-factory/references/v1-01-mvp1-acceptance-criteria.md`
  Depends on: 13
<!-- Commit checkpoint: tasks 11-14 -->
