# 0008. Keep `delegate_to_agent`; forward usage to the parent

- **Status:** Accepted
- **Date:** 2026-09-26
- **PR:** raniendu/vikram#43 (WS5), landed on `main` via raniendu/vikram#47

## Context

An agent can hand a task to another agent with `delegate_to_agent`. The harness
`SubAgents` capability does the same job differently.

## Options considered

| | `delegate_to_agent` | Harness `SubAgents` |
|---|---|---|
| When the child is built | On call, **after checking the surface**, so `coder` is never built on HTTP, threaded or Telegram | Every child built up front |
| Child asks for approval | Stops the child and tells the parent to run that agent directly | Fails with `UserError` |
| Child's `UserPromptSubmit`/`Stop` hooks | Run | Skipped |
| Child usage counted in the parent | No (before this change) | Yes |

## Decision

Keep `delegate_to_agent`, and adopt the one thing `SubAgents` did better: the
child runs with the parent's `RunUsage`, so its requests and tokens count in the
parent's usage and toward the parent's `spend_limits`.

## Consequences

- The `coder` local-only rule keeps holding for delegation.
- Budgets now include delegated work; a delegating run can hit its limit sooner
  than before.

## Revisit when

`SubAgents` supports lazy, surface-checked children and approval handoff.
