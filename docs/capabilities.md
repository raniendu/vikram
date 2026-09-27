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
| `prompt_injection` | Scans tool results (web pages, files, MCP output) for planted instructions, with local pattern detection and no network. `report` logs `prompt_injection_detected` (tool and risk level only); `block` also withholds high-risk results. | `mode` (`report`), `tools` (all) |
| `guardrails` | Redacts known secret formats (Anthropic, OpenAI, AWS, GitHub, Slack, Stripe and Google keys, JWTs, private keys) and refuses prompts with blocked keywords before the model is called. | `redact_secrets` (`input`, `output`, `tool_results`), `blocked_keywords` |
| `memory` | A notebook the agent writes to and reads across runs (`write_memory`, `read_memory`, `search_memory`, `delete_memory`), injected into each request. Stored in local SQLite. `conversation` keeps one notebook per chat, keyed by a hash of the conversation id. | `scope` (`conversation`), `database` (`.vikram/memory.sqlite3`), `max_tokens` (2000) |
| `spend_limits` | Stops a run that exceeds its budget (`SpendLimitExceeded`). Local models are priced at zero, so use `tokens_per_run` with Ollama. Budgets live in process memory. | `tokens_per_run`, `usd_per_run`, `usd_per_day`, `surfaces` (`http`, `telegram`, `threaded`) |

## Shipped defaults

Both shipped agents (`vikram` and `coder`) turn on the three low-risk
capabilities:

| Capability | Setting | Why |
|---|---|---|
| `repair_tool_arguments` | on | Local Ollama models often emit near-JSON tool calls. |
| `tool_output_limits` | 40,000 chars, head + tail | One cap for every tool: built-in, MCP and delegated. MCP output was previously uncapped, and `run_command`/`inspect_command` no longer cap their own output (they used to stop at 12,000 chars). A spec without this entry gets **uncapped** command output. |
| `compaction` | summarizing at 24,000 tokens, keep 20 | Long Telegram and API threads previously grew until the model's window overflowed, with only a warning at 85%. |

Both also run `prompt_injection` in **report** mode, so detections are logged
without changing answers. `vikram` redacts secrets from its replies
(`guardrails.redact_secrets = ["output"]`).

`spend_limits` is supported but off: local models cost nothing, and a
budget stops a run with an error. `memory` is off: it stores what the
model chooses to remember, so turning it on is a deliberate choice per agent.

**Ollama context size:** Ollama truncates prompts silently at the model's
`num_ctx`, which is often smaller than 24,000 tokens by default. If your model
runs with a small `num_ctx`, lower `max_tokens` below it so compaction happens
before Ollama cuts the prompt.

## Guarantees

- Approvals stay Vikram's. Capabilities never approve a tool call themselves.
  Where a harness tool needs approval, a guardrail defers it to the same
  prompt the CLI, ACP editor or GUI already shows.
- `coder` stays local-only. Capabilities don't change which surfaces an agent
  can run on.
- Logs name the capabilities, never their settings or content.
- Pydantic AI's first-run banner is disabled, because the CLI and ACP own stdout.

## Decision: skills stay on Vikram's `load_skill` (WS4)

The harness `Skills` capability was evaluated as a replacement for
`vikram/skills.py` and **not adopted** (harness 0.35.0, pydantic-ai-slim 2.51.0):

| | Vikram `load_skill` | Harness `Skills` |
|---|---|---|
| Tool the model calls | `load_skill(name)` | `load_capability` (Pydantic AI's deferred-capability loader). The name is **reserved by the framework and can't be renamed** |
| What loading returns | the skill body as a tool result, plus a list of the skill's **bundled files** (e.g. `conventional-commits/examples.md`) | the body becomes agent instructions; bundled files aren't mentioned |
| Spec layout | each spec lists individual skill folders | scans a *library* folder of skill folders |

Switching would rename a tool that the `vikram` and `coder` system prompts and
two eval cases (`coder.commit_message_skill`, `vikram.research_uses_skill`)
depend on, and would hide `examples.md` from the commit-message skill.
`vikram/skills.py` is small and fully tested, so it stays.

**Revisit when** Pydantic AI lets the deferred-loader tool be renamed, or the
harness lists bundled resources. Then it's a small swap in `build_agent`, and
the eval hook will score it as an `mcp_hooks_skills` change.

## Decision: commands keep Vikram's argv-only executor (WS3)

The harness `Shell` capability runs every command through `/bin/sh -c`.
Vikram's `run_command`/`inspect_command` execute an argument list with no
shell, and the command policy (read-only classification, the deny backstop,
Tier-2 approval) is written for that. Under a shell, `git status; rm -rf ~`,
`$(curl …)` or `> file` would become live, and a deny list on raw shell text
is, in the harness's own words, "best-effort guardrails, not security
boundaries". So the executor stays.

What was adopted instead: the harness's `LLM_API_KEY_ENV_PATTERNS`. Commands the
agent runs no longer inherit provider API keys or Vikram's own secrets.

**Revisit when** the harness offers argv execution, or Vikram runs commands
inside an OS sandbox (container or Modal), where a shell is safe to allow.

## Decision: delegation stays on Vikram's `delegate_to_agent` (WS5)

The harness `SubAgents` capability was evaluated and not adopted:

- It needs every child agent **built up front** as a raw Pydantic AI agent.
  Vikram builds the child only when it's called, after checking the surface
  (so `coder` is never even constructed on HTTP, threaded or Telegram).
- A child that asks for approval **fails the delegation** with a `UserError`.
  Vikram stops the child and tells the parent why ("run that agent directly").
- Running a raw agent skips the child's `UserPromptSubmit`/`Stop` hooks.

Adopted instead: **usage forwarding**. The child runs with the parent's
`RunUsage`, so delegated requests and tokens appear in the parent's usage and
count toward its `spend_limits`, as they do with `SubAgents`.

## Versions

`pydantic-ai-harness` is pinned exactly because it is pre-1.0. It requires
`pydantic-ai-slim>=2.44`. Upgrades are deliberate, and the eval hook records
them as a `framework` change.

## Migration roadmap

The migration ran as separate work streams, one PR each. **All are done** and
on `main`. The ADRs in [`docs/adr/`](adr/README.md) record why each ended the
way it did.

| Stream | What | Outcome | PR | ADR |
|---|---|---|---|---|
| WS8 | Tracing | OpenLIT replaced by the OpenTelemetry SDK (OpenLIT blocked the upgrade) | #37 | [0003](adr/0003-opentelemetry-sdk-instead-of-openlit.md) |
| WS0 | Foundation | Framework upgrade, harness added, `[capabilities]` table | #38 | [0004](adr/0004-adopt-pydantic-ai-harness-declaratively.md) |
| WS1 | Quick wins | Argument repair, output limits, compaction on in the shipped specs | #39 | [0004](adr/0004-adopt-pydantic-ai-harness-declaratively.md) |
| WS2 | File tools | Harness `FileSystem`, behind Vikram's guard | #40 | [0005](adr/0005-file-tools-on-harness-filesystem.md) |
| WS4 | Skills | Kept Vikram's `load_skill` | #41 | [0006](adr/0006-keep-load-skill.md) |
| WS3 | Shell | Kept the argv-only executor; commands no longer inherit secrets | #42 | [0007](adr/0007-keep-argv-command-executor.md) |
| WS5 | Delegation | Kept `delegate_to_agent`; child usage counts toward the parent | #43 → #47 | [0008](adr/0008-keep-delegate-to-agent.md) |
| WS7 | Safety and memory | Injection scan (report mode), secret redaction; memory opt-in | #44 → #47 | [0009](adr/0009-injection-defence-and-memory-opt-in.md) |
| WS6 | Durable runs | `VIKRAM_DURABLE_AGENT_RUNS` checkpoints threaded runs in DBOS; on by default since ADR 0012 | #45 → #47 | [0010](adr/0010-durable-agent-runs-opt-in.md), [0012](adr/0012-durable-agent-runs-on-by-default.md) |

Follow-ups (blocking prompt injections, a `[capabilities]` editor in the
desktop app) are tracked in [backlog.md](backlog.md).
