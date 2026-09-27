# 0006. Keep Vikram's `load_skill` instead of harness `Skills`

- **Status:** Accepted
- **Date:** 2026-09-26
- **PR:** raniendu/vikram#41 (WS4)

## Context

A skill is a folder of instructions the agent loads only when needed. Vikram's
`load_skill(name)` returns the skill's body plus a list of its bundled files
(e.g. `conventional-commits/examples.md`). The harness has a `Skills`
capability that could replace it.

## Options considered

| | Vikram `load_skill` | Harness `Skills` |
|---|---|---|
| Tool the model calls | `load_skill` | `load_capability`: reserved by the framework, **can't be renamed** |
| Bundled files | Listed | Not mentioned |
| Spec layout | Lists skill folders | Scans a library folder |

## Decision

Keep `vikram/skills.py`. Switching would rename a tool that both agents' system
prompts and two eval cases (`coder.commit_message_skill`,
`vikram.research_uses_skill`) rely on, and hide `examples.md` from the
commit-message skill, for no gain.

## Consequences

- No behaviour change.
- We keep maintaining a small, fully tested loader.

## Revisit when

Pydantic AI lets the deferred-loader tool be renamed, or the harness lists a
skill's bundled files. The swap is then small, and evals score it as an
`mcp_hooks_skills` change.
