# 0010. Durable agent runs on DBOS, off by default

- **Status:** Proposed
- **Date:** 2026-09-26
- **PR:** raniendu/vikram#45 (WS6), landing on `main` via raniendu/vikram#47

## Context

Each Telegram/threaded message is a DBOS workflow, so a crash never loses a
message. But a restarted workflow **redoes the whole agent run**: every model
call is made again, which costs time and tokens and can give a different
answer.

## Decision

Add Pydantic AI's `DBOSDurability` capability to threaded agents when
`VIKRAM_DURABLE_AGENT_RUNS=true` (default **false**). Model requests, MCP calls
and capability operations (e.g. the compaction summary) become DBOS steps, so a
restarted workflow replays finished steps from the journal and continues.

## Consequences

- A crash mid-run resumes instead of starting over (proven by a gated test that
  forks a finished run from its last step).
- Plain function tools (`web_search`, `delegate_to_agent`) are not steps; they
  run again on replay.
- A release that changes an agent's steps can't resume runs journaled by the
  previous release; drain in-flight runs before such deploys.
- Outside a workflow (CLI, ACP, `/chat`) it does nothing.

## Revisit when

It has run in a staging deployment without replay problems: make it the default.
