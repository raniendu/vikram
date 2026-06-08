# Tools

Agent specs reference tool names from `vikram.tools.TOOL_REGISTRY`. Agents can
also gain tools from external MCP servers (`[[mcp_servers]]`) and on-demand
instruction packs (skills); both are documented in
[mcp_and_skills.md](mcp_and_skills.md).

## `web_search`

Uses `PARALLEL_API_KEY` and returns compact source-backed search results. Specs
should use it when current or externally verifiable facts matter.

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
