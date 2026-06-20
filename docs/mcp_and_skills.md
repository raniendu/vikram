# Using MCP servers and skills

Vikram agents can be extended two ways, both declared per agent in
`spec/<agent>/agent.toml` and assembled into the agent by
`vikram.agent.build_agent`:

- **Skills** give the agent *new instructions* — reusable, expert task
  playbooks that are loaded only when relevant.
- **MCP servers** give the agent *new tools* from an external Model Context
  Protocol server (local subprocess or remote HTTP endpoint).

Both apply on every surface the agent runs on (CLI, ACP, HTTP `/chat`, threaded
queues, Telegram), because every surface builds the agent through the same
`build_agent`. An agent marked `cli_only = true` (like `coder`) still uses its
MCP servers and skills — just only on the CLI/ACP surfaces it is allowed on.

`vikram` also acts as an orchestrator. Its `delegate_to_agent` tool can run
another checked-in agent with a self-contained prompt and return that subagent's
report. Surface restrictions still apply, so `coder` can be delegated to from
local CLI/ACP sessions but not through HTTP, threaded, or Telegram runs.

## Inspect what an agent has

You don't need a configured model to see what a spec will attach. Run this from
the repo root to list an agent's skills and MCP servers (this also expands
`${ENV_VAR}` references, so it surfaces missing-variable errors early):

```bash
uv run python -c "
from vikram.settings import VikramSettings
from vikram.spec import load_spec
from vikram.skills import discover_skills
from vikram.mcp import build_mcp_servers

spec = load_spec('vikram', VikramSettings().spec_root)
print('skills:     ', [s.name for s in discover_skills(spec)])
print('mcp servers:', [s.id for s in build_mcp_servers(spec.mcp_servers)])
"
```

For the built-in `vikram` agent this prints the `web-research` skill and no MCP
servers. Swap `'vikram'` for `'coder'` to inspect that agent.

When you run the agent interactively (`uv run vikram` or
`uv run vikram --agent coder`), the CLI prints each tool call as it happens —
`→ load_skill(name="web-research")` followed by `✓ load_skill`, or
`→ delegate_to_agent(agent_name="coder", ...)` for orchestration — so you can
watch skills, subagents, and MCP tools being used in real time.

---

## Skills

A skill is a folder of expert instructions for one kind of task, following the
Agent Skills convention. Skills use **progressive disclosure**: only each
skill's `name` and one-line `description` are injected into the agent's system
instructions up front. The full body is loaded on demand when the agent calls
the `load_skill` tool, which Vikram adds automatically to any agent that has at
least one skill.

### What happens at runtime

1. At build time, `build_agent` loads every configured skill and adds an
   `## Available skills` block to the instructions listing each skill as
   `- **name**: description`.
2. The model reads a user request, matches it to a skill by description, and
   calls `load_skill(name="<skill>")`.
3. `load_skill` returns the skill's full Markdown body plus a list of any
   bundled resource files.
4. The model follows those instructions for the rest of the turn.

This keeps the base prompt small even with many skills installed, and means a
skill's detailed steps only consume context when actually needed.

### Walkthrough: add a skill to an agent

This adds a `release-notes` skill to the `vikram` agent.

1. Create the skill folder and `SKILL.md` under the agent's spec directory:

   ```bash
   mkdir -p spec/vikram/skills/release-notes
   ```

   `spec/vikram/skills/release-notes/SKILL.md`:

   ```markdown
   ---
   name: release-notes
   description: Turn a list of merged changes into clear, grouped release notes. Use when the user asks for release notes, a changelog, or a "what's new" summary.
   ---

   # Release notes

   1. Group changes into Features, Fixes, and Maintenance.
   2. Write each entry as a user-facing outcome, not an implementation detail.
   3. Lead with the most impactful change in each group.
   4. Call out breaking changes in a dedicated section at the top.
   ```

2. Reference it from `spec/vikram/agent.toml` by **setting the existing
   `skills` key** (the spec already ships `skills = []`; replace it rather than
   adding a second `skills` line, which would be a TOML duplicate-key error).
   Paths in `skills` are relative to the agent's own spec directory
   (`spec/vikram/`):

   ```toml
   skills = ["skills/release-notes"]
   ```

3. Verify it loads (see [Inspect what an agent has](#inspect-what-an-agent-has))
   — `release-notes` should appear in the `skills:` list.

4. Run the agent and ask for release notes; watch for the
   `→ load_skill(name="release-notes")` call:

   ```bash
   uv run vikram
   ```

### `SKILL.md` format

```markdown
---
name: web-research
description: A method for answering questions that need current or source-backed facts. Use when the user asks about recent events, prices, or anything to verify.
---

# Web research

Step-by-step instructions the agent follows once the skill is loaded...
```

- **`description` is required.** It is the only part of the skill the model sees
  before loading it, so write it to make relevance obvious — state both *what*
  the skill does and *when* to use it.
- **`name` is optional** and defaults to the skill's directory name. Names must
  be unique within an agent.
- Frontmatter is parsed leniently (a simple `key: value` block between `---`
  fences); a value may itself contain colons.

### Bundled resources

Any other files in a skill directory are reported to the model as bundled
resources when the skill is loaded. The agent can then read them with its file
tools. If a target agent lacks direct file tools, keep its skills self-contained.

```
spec/coder/skills/conventional-commits/
  SKILL.md        # the skill
  examples.md     # listed as a bundled resource when the skill is loaded
```

### Configuration reference

```toml
# Agent-local skills: paths relative to this agent's spec dir (e.g. spec/coder/).
skills = ["skills/conventional-commits"]

# Shared skills: paths relative to spec/shared/. Use these to share one skill
# across several agents.
shared_skills = ["skills/web-research"]
```

A path may point at a skill directory (containing `SKILL.md`) or directly at a
`.md` file. Local skills load before shared skills.

### Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `SkillError: ... missing a 'description'` | Add a `description:` line to the skill's frontmatter. |
| `SkillError: ... has no instructions body` | The file has frontmatter but no body text; add the instructions. |
| `SkillError: ... is missing a SKILL.md file` | The path points at a directory with no `SKILL.md`. Add one, or point at a `.md` file. |
| `SkillError: Duplicate skill name` | Two configured skills resolve to the same name. Rename one (or set distinct `name:` values). |
| The agent never calls `load_skill` | The description doesn't clearly signal when to use the skill. Make it more specific about the trigger. |

---

## MCP servers

Each `[[mcp_servers]]` entry becomes a Pydantic AI MCP toolset attached to the
agent. The server's tools appear to the model alongside the agent's built-in
tools. Pydantic AI starts and stops the server automatically for each agent
run, so there is nothing to manage manually. In interactive CLI mode, Vikram
keeps the servers connected for the whole session (instead of restarting them
each turn) whenever the spec declares any MCP servers.

### Prerequisites

The example servers below are launched with `npx` (needs Node.js installed) or
`uvx` (ships with `uv`, which this project already uses). For HTTP/SSE servers
you only need network access to the endpoint.

### Walkthrough: add an MCP server

This adds the GitHub MCP server to an agent.

1. Choose a transport (see [Transports](#transports)). For a local tool server
   that's `stdio`.

2. Add the entry to the agent's `agent.toml`. Reference any secret as
   `${ENV_VAR}` rather than pasting it inline:

   ```toml
   [[mcp_servers]]
   name = "github"
   transport = "stdio"
   command = "npx"
   args = ["-y", "@modelcontextprotocol/server-github"]
   env = { GITHUB_PERSONAL_ACCESS_TOKEN = "${GITHUB_TOKEN}" }
   tool_prefix = "gh"
   ```

3. Provide the referenced secret in the environment or `.env`:

   ```bash
   echo 'GITHUB_TOKEN=ghp_xxx' >> .env
   ```

4. Verify the server builds and the token expands (see
   [Inspect what an agent has](#inspect-what-an-agent-has)) — `github` should
   appear in the `mcp servers:` list with no error.

5. Run the agent. Its GitHub tools are exposed as `gh_*` (because of
   `tool_prefix`), e.g. `gh_search_repositories`.

### Transports

| `transport` | Use for | Required fields |
| --- | --- | --- |
| `stdio` (default) | A local server you launch as a subprocess | `command` |
| `http` | A remote streamable-HTTP MCP endpoint | `url` |
| `sse` | A remote Server-Sent-Events MCP endpoint | `url` |

### Field reference

| Field | Transport | Meaning |
| --- | --- | --- |
| `name` | all | Required. Stable id; used in errors and as the toolset id. Unique per agent. |
| `transport` | all | `stdio` (default), `http`, or `sse`. |
| `tool_prefix` | all | Optional. Namespaces this server's tools as `<prefix>_<tool>`, avoiding name clashes. |
| `timeout` | all | Connection/init timeout in seconds (default 5). |
| `read_timeout` | all | Optional per-request read timeout in seconds. |
| `command` | stdio | Required for stdio. Executable to launch. |
| `args` | stdio | Argument list for the command. |
| `env` | stdio | Environment for the subprocess. Omit to inherit the parent process env. |
| `cwd` | stdio | Working directory for the subprocess. |
| `url` | http/sse | Required for http/sse. MCP endpoint URL. |
| `headers` | http/sse | HTTP headers sent to the endpoint (e.g. `Authorization`). |

### Secrets and `${ENV_VAR}` expansion

Never write secrets inline. Any string field — `command`, `args`, `url`, `cwd`,
and the values of `env` and `headers` — may contain `${VAR}` references, which
are expanded from the process environment when the agent is built. A reference
to an undefined variable is a hard error (`MCPConfigError`) naming the field, so
misconfiguration fails loudly at startup rather than silently sending an empty
token. This keeps specs safe to commit while real credentials stay in `.env` or
the runtime environment.

### More examples

```toml
# Local stdio server with no secrets (inherits the parent environment).
[[mcp_servers]]
name = "fetch"
transport = "stdio"
command = "uvx"
args = ["mcp-server-fetch"]

# Remote streamable-HTTP server behind a bearer token.
[[mcp_servers]]
name = "docs"
transport = "http"
url = "https://mcp.example.com/mcp"
headers = { Authorization = "Bearer ${DOCS_MCP_TOKEN}" }
```

### Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `MCPConfigError: ... references undefined environment variable ${X}` | Set `X` in `.env` or the environment before running. |
| `MCPConfigError: ... uses stdio transport but has no 'command'` | Add `command` (and usually `args`) for stdio servers. |
| `MCPConfigError: ... uses http transport but has no 'url'` | Add `url` for http/sse servers. |
| `MCPConfigError: Duplicate MCP server name` | Each `[[mcp_servers]]` `name` must be unique within an agent. |
| Server fails to start at run time | The `command` isn't installed or isn't on `PATH` (e.g. Node.js for `npx`). Confirm you can run it manually. |
| Two servers expose same-named tools | Give at least one a `tool_prefix` to disambiguate. |

---

## Built-in examples

The repository ships working examples you can copy:

- `spec/shared/skills/web-research/` — a shared skill wired into `vikram` via
  `shared_skills`.
- `spec/coder/skills/conventional-commits/` — an agent-local skill (with a
  bundled `examples.md`) wired into `coder` via `skills`.
- Commented `[[mcp_servers]]` blocks at the bottom of `spec/vikram/agent.toml`
  and `spec/coder/agent.toml`.
