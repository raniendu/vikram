# 0004. Adopt Pydantic AI Harness through a declarative `[capabilities]` table

- **Status:** Accepted
- **Date:** 2026-09-26
- **PR:** raniendu/vikram#38 (WS0), raniendu/vikram#39 (WS1 defaults)

## Context

[Pydantic AI Harness](https://github.com/pydantic/pydantic-ai-harness) offers
ready-made agent behaviours ("capabilities"): context compaction, tool output
limits, repair of malformed tool calls, budgets, guardrails, memory. Vikram
already had hand-written versions of a few, and was missing the rest. Long
Telegram and API threads grew until they overflowed the model's context window.

Vikram agents are defined by specs (`agent.toml`), not code.

## Decision

- Add `pydantic-ai-harness`, **pinned exactly** (0.35.0) because it is pre-1.0,
  and upgrade `pydantic-ai-slim` 2.31 → 2.51.
- Expose capabilities as an optional `[capabilities]` table in `agent.toml`,
  validated strictly (a misspelt key is an error). An absent entry is off, so a
  spec without the table builds the same agent as before.
- Capabilities are added after Vikram's approval handler: they never approve a
  tool call themselves, and they don't change which surfaces an agent runs on.
- Ship both agents with the three low-risk ones on: `repair_tool_arguments`,
  `tool_output_limits` (40,000 chars), `compaction` (summarizing at 24,000
  tokens, keep 20 messages).

## Consequences

- New behaviours are a spec edit, and the eval hook scores them as a
  `capabilities` change.
- Two framework changes had to be handled: `result.usage` became an attribute
  (the context warning read it as a method and went silent), and Pydantic AI's
  first-run banner would print on the CLI/ACP stdout (now disabled).
- Harness upgrades are deliberate and show up as `framework` changes in evals.
- Ollama may truncate at a `num_ctx` below 24,000 tokens; `max_tokens` must be
  set below it for such models.

## Revisit when

The harness reaches 1.0 (relax the exact pin), or a capability we turned on
shows a regression in evals.
