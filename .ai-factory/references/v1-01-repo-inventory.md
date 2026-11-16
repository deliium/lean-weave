# v1-01 Repository Inventory

Logged: `[Inventory.run]` via `LEAN_WEAVE_LOG_LEVEL` / `LOG_LEVEL` (default INFO).

## Stack facts

| Fact | Value |
|------|-------|
| Package | `lean-weave` 0.1.0 (`pyproject.toml`) |
| Python | `>=3.12` (`.python-version` = 3.12) |
| Build | `uv_build` backend; `uv.lock` present; local `.venv/` |
| Runtime deps | none declared yet |
| Console script | `lean-weave = lean_weave:main` |
| Git | **absent** (no `.git`) |
| Sample entry | `src/main.py` — PyCharm template (`print_hi`), **not** wired to the package script |
| Package entry | `src/lean_weave/__init__.py` — `main()` prints `Hello from lean-weave!` |

**Entrypoint tension (defer resolution to Task 11):** `project.scripts` points at `lean_weave:main`, while `src/main.py` is an unrelated IDE sample. Canonical CLI should be the package entrypoint; `src/main.py` is a candidate for removal or rehome outside the product path.

## Layout snapshot (file counts)

| Area | Text files counted | Notes |
|------|--------------------|-------|
| `src/` | 2 | `main.py`, `lean_weave/__init__.py` |
| `.cursor/skills/` | 157 | 30 aif-* skills + references |
| `.opencode/skills/` | 157 | Mirror of Cursor skills |
| `.ai-factory/` | 3 (+ growing) | `config.yaml`, `plans/v1-01-architecture.md`, `skill-context/aif-plan/SKILL.md` |
| Repo root files | 5 | `pyproject.toml`, `README.md` (empty), `uv.lock`, `.python-version`, `.ai-factory.json` |
| Skipped | `.venv/`, `.idea/` | dependency / IDE noise |

Inventory complete: ~324 tracked text paths across listed areas (skills dominate).

## Missing AI Factory core artifacts

| Artifact | Path | Status |
|----------|------|--------|
| DESCRIPTION | `.ai-factory/DESCRIPTION.md` | missing (produced by this plan, Task 12) |
| ARCHITECTURE | `.ai-factory/ARCHITECTURE.md` | missing (Task 13) |
| ROADMAP | `.ai-factory/ROADMAP.md` | missing |
| RESEARCH | `.ai-factory/RESEARCH.md` | missing |
| RULES / rules tree | `.ai-factory/RULES.md`, `rules/base.md` | missing |
| AGENTS.md | repo root | missing |
| references/ | `.ai-factory/references/` | created by this plan |
| adr/ | `.ai-factory/adr/` | created by this plan |

Config exists (`.ai-factory/config.yaml`) with plan defaults: testing=yes, logging=verbose, docs=yes; git.enabled=true but no repository yet.

## Skill inventory (pipeline relevance)

Sizes measured under `.cursor/skills/` (`.opencode/skills/` is a duplicate install surface — do not load both).

| Skill | Role for lean-weave pipeline | Approx. tree bytes | Recommendation |
|-------|------------------------------|--------------------|----------------|
| `aif-plan` | Plan artifact shape, settings, tasks, reconciliation | 73 KB | **Reuse methodology** (templates/procedures only) |
| `aif-implement` | Sequential task execution, checkboxes, checkpoints | 53 KB | **Reuse methodology** (no nested agents) |
| `aif-verify` | Verification gates → VerificationRunner mapping | 43 KB | **Reuse gate list** (deterministic subset) |
| `aif-commit` | Conventional commits / checkpoint messaging | 14 KB | Optional later; human/out-of-band in MVP docs |
| `aif-warmup` | Compact context handoff pattern | 6 KB | **Reuse idea** (stage digests, not full skills) |
| `aif` | Project AI context bootstrap | 62 KB | Deferred (setup already done) |
| `aif-architecture` | Architecture doc generation | 59 KB | Deferred (this plan authors ARCHITECTURE directly) |
| `aif-docs` | Docs pages / README table | 31 KB | Used at docs checkpoint (Task 14) |
| `aif-best-practices` | Coding conventions | 9 KB | Optional reference for later code plans |
| `aif-security-checklist` | Security review | 35 KB | Feed isolation/security note; not wholesale load |
| `aif-review` / `aif-qa*` / `aif-rules*` | Review & QA loops | varies | Deferred past MVP 1 architecture gate |
| `aif-dockerize` / `aif-ci` / `aif-loop` / `aif-explore` / … | Infra, agents, exploration | large | **Out of MVP 1** (Docker/workers/multi-agent forbidden) |

**Pipeline-relevant subset** (`plan`+`implement`+`verify`+`commit`+`warmup`) ≈ 190 KB on disk — still too large to inject wholesale into LLM context.

## Token-relevant observations

1. Full `.cursor/skills/` tree ≈ **1.29 MB**; all `SKILL.md` files alone ≈ **647 KB**. Loading skill trees into prompts is incompatible with the token-efficiency goal.
2. Duplicate skill installs (Cursor + OpenCode) must never both be loaded.
3. Prefer **extracted procedure fragments** and **compact templates** (Task 3–4) over raw skill markdown.
4. Greenfield app code is tiny; almost all token risk is instruction/context bloat, not application source.
5. Empty `README.md` and placeholder `pyproject` description — documentation surfaces are owned by Tasks 12–14.

## Canonical commands (Task 2)

Documented in `spikes/README.md`. Bootstrap applied: `pyproject.toml` dependency groups (`dependencies`+`spikes`+`dev`), `.gitignore` (36 ignore rules), `uv sync --all-groups` succeeded.

```bash
uv sync --all-groups
uv run pytest tests/spikes tests/schemas tests/contracts
```

Packages left with lower-bound pins only so spikes track current DeepSeek/LangGraph APIs.
