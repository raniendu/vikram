# Tools

Agent specs reference tool names from `vikram.tools.TOOL_REGISTRY`, plus the
special orchestration tool `delegate_to_agent`. Agents can also gain tools from
external MCP servers (`[[mcp_servers]]`) and on-demand instruction packs
(skills); both are documented in [mcp_and_skills.md](mcp_and_skills.md).

Tool calls can be observed or blocked with `PreToolUse` and `PostToolUse`
hooks. Hooks are configured separately from tools; see [hooks.md](hooks.md).

## `web_search`

Uses `PARALLEL_API_KEY` and returns compact source-backed search results. Specs
should use it when current or externally verifiable facts matter.

## `delegate_to_agent`

Lets an orchestrator agent call another checked-in Vikram agent with a
self-contained prompt. The built-in `vikram` spec uses this to delegate
specialized work, such as repository tasks, to `coder` instead of directly
owning every tool itself.

Delegation is visible as a normal tool call in interactive UIs. In the CLI, a
user sees a call such as `→ delegate_to_agent(agent_name="coder", ...)` and can
approve or deny it before the subagent runs. Once approved, the delegated run
may use the target agent's approval-gated tools; command policy deny rules still
apply as a hard backstop.

`cli_only` agents remain local-only. For example, `vikram` can delegate to
`coder` from CLI/ACP sessions, but HTTP, threaded, and Telegram runs cannot use
delegation to bypass `coder`'s surface restriction.

## Local Coding Tools

The `coder` spec enables these CLI-only tools:

| Tool | Behavior |
| --- | --- |
| `read_file` | Read a numbered UTF-8 excerpt within cwd |
| `glob` | List files under cwd while skipping caches and sensitive paths |
| `grep` | Regex search under cwd while skipping caches and sensitive paths |
| `inspect_command` | Run read-only commands accepted by command policy |
| `write_file` | Write a file after human approval |
| `edit_file` | Exact-text replace after human approval |
| `run_command` | Run argv-only commands with policy-based approval or denial |

Command policy lives in `spec/shared/command_policy.toml`. Deny rules are a
hard backstop and cannot be bypassed by approval.

## `load_skill`

Added automatically to any agent that has skills configured. It takes a skill
`name` and returns that skill's full instructions plus a listing of its bundled
resource files. See [mcp_and_skills.md](mcp_and_skills.md).
