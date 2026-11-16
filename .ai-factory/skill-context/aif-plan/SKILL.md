# Plan Preference Defaults

When creating a new plan, read `.ai-factory/config.yaml` workflow preferences:

- `workflow.plan_testing` — `yes` | `no` | `ask`
- `workflow.plan_logging` — `verbose` | `standard` | `minimal` | `ask`
- `workflow.plan_docs` — `yes` | `no` | `ask`
- `workflow.plan_link_roadmap` — `true` | `false` | `ask`

## Application

1. If a preference is set to a concrete value (not `ask`), use it as the plan
   Settings default and do **not** re-ask that question unless the user
   overrides it in the current request.
2. If a preference is `ask` or missing, keep the interactive AskUserQuestion flow
   from the base skill.
3. Always write the resolved Settings into the plan entrypoint.
4. Handoff mode (`HANDOFF_MODE=1`) still uses non-interactive defaults and ignores
   interactive prompts; config values override the skill's built-in Handoff
   defaults when present.

Current project defaults: testing=yes, logging=verbose, docs=yes.
