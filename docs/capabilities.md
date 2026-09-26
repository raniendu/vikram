# Capabilities (Pydantic AI Harness)

Vikram builds its agents on [Pydantic AI](https://ai.pydantic.dev). The
[Pydantic AI Harness](https://github.com/pydantic/pydantic-ai-harness) adds
ready-made **capabilities**: self-contained pieces of agent behaviour such as
keeping long chats inside the context window, capping tool output, or budgets.
Each one is an object added to `Agent(capabilities=[...])`.

Vikram exposes them declaratively. An agent's `agent.toml` can hold a
`[capabilities]` table. Every entry is optional, and an entry that is absent is
**off**, so a spec without the table builds exactly the agent it always did.

```toml
[capabilities]
repair_tool_arguments = true

[capabilities.compaction]
strategy = "summarizing"
max_tokens = 24000
```

`vikram/capabilities.py` validates the table (a misspelt key is an error, not a
silently disabled feature) and `build_agent` adds the capabilities after
Vikram's own approval handler. The `agent_built` log line lists the capability
names that were attached.

## Reference

| Key | What it does | Settings (defaults) |
|---|---|---|
| `repair_tool_arguments` | Fixes malformed tool-call JSON (trailing commas, single quotes, truncated JSON) before validation. Helps local models. | `true` / absent |
| `tool_output_limits` | Caps every tool result, built-in or MCP, before the model sees it. Oversized output is truncated, never written to disk. | `max_chars` (40000), `strategy` `head` \| `tail` \| `head_tail` |
| `compaction` | Keeps long conversations inside the context window. `summarizing` replaces older turns with a summary written by the agent's own model; `sliding_window` drops the oldest turns. | `strategy` (`summarizing`), `max_tokens` (24000) trigger, `keep_messages` (20) |
| `spend_limits` | Stops a run that exceeds its budget (`SpendLimitExceeded`). Local models are priced at zero, so use `tokens_per_run` with Ollama. Budgets live in process memory. | `tokens_per_run`, `usd_per_run`, `usd_per_day`, `surfaces` (`http`, `telegram`, `threaded`) |

## Guarantees

- Approvals stay Vikram's. Capabilities never approve a tool call themselves.
  Where a harness tool needs approval, a guardrail defers it to the same
  prompt the CLI, ACP editor or GUI already shows.
- `coder` stays local-only. Capabilities don't change which surfaces an agent
  can run on.
- Logs name the capabilities, never their settings or content.
- Pydantic AI's first-run banner is disabled, because the CLI and ACP own stdout.

## Versions

`pydantic-ai-harness` is pinned exactly because it is pre-1.0. It requires
`pydantic-ai-slim>=2.44`. Upgrades are deliberate, and the eval hook records
them as a `framework` change.

## Migration roadmap

The migration runs as separate work streams, each in its own PR:

| Stream | What | Status |
|---|---|---|
| WS8 | Tracing on the OpenTelemetry SDK (removes OpenLIT, which blocked the upgrade) | PR |
| WS0 | This foundation: framework upgrade, `[capabilities]` table | PR |
| WS1 | Turn on repair, output limits and compaction in the shipped specs | planned |
| WS2 | File tools on harness `FileSystem` | planned |
| WS4 | Skills on harness `Skills` | planned |
| WS3 | Shell on harness `Shell`, with the command policy as a guardrail | planned |
| WS5 | Delegation on harness `SubAgents` | planned |
| WS7 | Prompt-injection defender, guardrails, memory | planned |
| WS6 | Durable agent runs on DBOS | planned |
