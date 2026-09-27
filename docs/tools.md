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
| `read_file` | Read a numbered excerpt within cwd (`path`, `offset`, `limit`; 200 lines max) |
| `glob` | Find files under cwd by glob pattern, skipping caches and sensitive paths |
| `grep` | Regex search under cwd with ripgrep (context lines, case, file type), skipping caches and sensitive paths |
| `inspect_command` | Run read-only commands accepted by command policy |
| `write_file` | Write a file after human approval, creating missing folders |
| `edit_file` | Replace one unique text fragment (`old_text` → `new_text`) after human approval |
| `run_command` | Run argv-only commands with policy-based approval or denial |

The file tools (`read_file`, `glob`, `grep`, `write_file`, `edit_file`) are
served by the Pydantic AI Harness `FileSystem` capability, wrapped by
`vikram/file_tools.py` so the names and guarantees above stay the same:

- **Confinement:** paths are resolved from cwd, and `..`, absolute paths and
  symlinks that leave it are refused.
- **Secrets and excluded folders:** `.env*` (except `.env.example`), keys,
  `.ssh/`, `secrets/`, Terraform state, `.git/`, `.venv/`, `node_modules/` and
  caches can't be read, written or listed.
- **Refusals are answers, not errors:** the tool returns `Refusing: …` and logs
  `tool_call_refused` with a stable reason, so a model that keeps asking for a
  secret can't crash the run.
- **Approvals and hooks:** writes go through the same approval prompt as
  before, and `PreToolUse`/`PostToolUse` hooks wrap these tools too.
- **ripgrep:** `grep` uses the `rg` binary installed with Vikram; if `rg` isn't
  on `PATH`, Vikram appends its own install folder to `PATH`.

Commands run **without a shell**: the command string is split with `shlex` and
executed as an argument list, so `;`, `&&`, pipes, `$(...)` and globbing are
inert. The command policy relies on that. Commands also **don't inherit
credentials**: provider API keys (`ANTHROPIC_*`, `OPENAI_*`, `GEMINI_*`,
`GOOGLE_*`, `OPENROUTER_*`, `GATEWAY_*`), every `VIKRAM_*` variable, and
`OLLAMA_API_KEY`, `PARALLEL_API_KEY`, `SARVAM_API_KEY`,
`DIGITALOCEAN_ACCESS_TOKEN` are removed from their environment.
`GITHUB_TOKEN`/`GH_TOKEN` are kept for `gh`.

Command policy lives in `spec/shared/command_policy.toml`. Deny rules are a
hard backstop and cannot be bypassed by approval.

## `load_skill`

Added automatically to any agent that has skills configured. It takes a skill
`name` and returns that skill's full instructions plus a listing of its bundled
resource files. See [mcp_and_skills.md](mcp_and_skills.md).
