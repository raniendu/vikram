# Vikram

Vikram is a public, standalone agent runtime built on Pydantic AI. It keeps
agent behavior in versioned specs under `spec/`, exposes the same agents through
CLI, HTTP, threaded queues, Telegram webhooks, and ACP, and ships safe local
coding tools for the CLI-only `coder` agent.

## Features

- Spec-driven agents: `spec/<agent>/agent.toml` plus Markdown prompts.
- Built-in agents: `vikram` for orchestration/general assistance and `coder`
  for local coding.
- CLI: interactive chat, one-shot prompts, JSON output, and self-update.
- ACP: editor integration for the local `coder` agent.
- HTTP: stateless `/chat`, durable `/threads/...`, `/events/...`, and health.
- Telegram: env-driven bot config, allowlist, group mention/reply routing, and
  `/reset` plus `/agent` commands.
- Tools: Parallel web search, subagent delegation, safe file/search/edit tools,
  and argv-only command execution guarded by a declarative command policy.
- MCP: attach external Model Context Protocol tool servers per agent via
  `[[mcp_servers]]`, with `${ENV_VAR}` secret references and automatic lifecycle.
- Skills: progressive-disclosure instruction packs under `spec/.../skills/`,
  surfaced by name/description and loaded on demand through `load_skill`.
- Hooks: run external commands or Python callables at lifecycle events
  (`PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop`) to observe, augment,
  or block what the agent does, via `[[hooks]]`.
- Runtime state: local SQLite for thread history and DBOS workflow state.
- Observability: structured JSON logs and optional OpenLIT/OpenTelemetry traces.

## Quick Start

```bash
uv sync
uv run vikram configure
uv run vikram --once --prompt "say pong"
```

Vikram does not ship with a default model provider or model name. Run
`vikram configure` (alias: `vikram setup`) once after installing: it walks a
menu of providers, lets you configure as many as you want in one session
(model + API key per provider), and asks which one is the default. Settings
are stored in `~/.config/vikram/config.toml`; environment variables and `.env`
still override that local file for development and deployment.

The wizard is safe to re-run at any time — it merges into the existing file,
so adding or updating one provider never discards the others. Package updates
(`vikram update` or the installer) never touch it either.

### Model providers

| Provider id | Backend | API key env var |
| --- | --- | --- |
| `ollama` | Local Ollama | — |
| `ollama-cloud` | [ollama.com](https://ollama.com) hosted models | `OLLAMA_API_KEY` |
| `anthropic` | Anthropic Claude | `ANTHROPIC_API_KEY` |
| `gemini` | Google Gemini | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) |
| `openai` | OpenAI | `OPENAI_API_KEY` |
| `digitalocean` | DigitalOcean serverless inference | `DIGITALOCEAN_ACCESS_TOKEN` |
| `openai-compatible` | Any OpenAI-compatible endpoint (e.g. Sarvam AI) | `VIKRAM_OPENAI_COMPAT_API_KEY` |

The resulting config looks like:

```toml
config_version = 2
default_provider = "anthropic"

[providers.anthropic]
model = "claude-sonnet-5"
api_key = "sk-ant-..."

[providers.ollama]
model = "llama3.2"
base_url = "http://localhost:11434"
```

Model resolution per agent, highest to lowest: `VIKRAM_MODEL_PROVIDER` /
`VIKRAM_MODEL` / `-m` (one run) → your saved per-agent choice
(`[agents.<name>]`, written by the in-session `/model` command) → the agent
spec's pinned model → the config file's `default_provider` and that
provider's model. An agent that pins its own model (like `coder`) therefore
keeps it even when the global default points elsewhere, until you switch it
with `/model`.

For local Ollama, pull a model you want to use before configuring it:

```bash
ollama pull <model-tag>
ollama serve
```

Equivalent `.env` settings for local Ollama:

```env
VIKRAM_MODEL_PROVIDER=ollama
VIKRAM_MODEL=<model-tag>
OLLAMA_BASE_URL=http://localhost:11434/v1
```

Equivalent `.env` settings for a hosted provider (Anthropic shown; use the
matching key env var from the table for the others):

```env
VIKRAM_MODEL_PROVIDER=anthropic
VIKRAM_MODEL=claude-sonnet-5
ANTHROPIC_API_KEY=...
```

## CLI

```bash
uv run vikram configure   # multi-provider setup wizard (alias: vikram setup)
uv run vikram
uv run vikram --agent coder
uv run vikram exec --agent coder "summarize this repo"
git diff | uv run vikram exec "review this patch"
uv run vikram exec -C ../another-repo -m <model-tag> "run the tests"
uv run vikram exec "write release notes" -o release-notes.md
uv run vikram doctor --agent coder
uv run vikram --once --prompt "summarize this repo"
uv run vikram --once --prompt @prompt.txt --json
vikram update --check
```

`vikram exec [PROMPT]` is the preferred non-interactive interface. When the
positional prompt is omitted it reads stdin. When both are present, stdin is
added as context for the positional instruction. `-C/--cd` selects the working
directory before configuration and specs are loaded, `-m/--model` overrides the
model for that run, and `-o/--output-last-message` also saves the final reply to
a file. The older `--once --prompt` form remains supported for existing scripts.

Interactive sessions support `/status` for the active agent, model, directory,
and context usage, `/model` to switch models without leaving the session —
`/model` alone opens a numbered selector over your configured providers, or
type `/model anthropic`, `/model ollama qwen3`, or `/model <model>` directly.
A switch keeps the conversation history and is saved as that agent's default
for future sessions. `/diff` inspects the working tree, `/copy` copies the
last reply, and `/new` starts a fresh conversation without exiting. Run
`vikram doctor` when setup or agent loading is not behaving as expected; it
checks configuration, specs, model selection, credentials without printing
their values, command policy, Python, and the current Git workspace.

The CLI UX research and implementation rationale are documented in
[docs/cli_ux_research.md](docs/cli_ux_research.md).

The `coder` agent is CLI-only. It can read/search files, request approval for
edits, and run commands through `spec/shared/command_policy.toml`. CLI-only
specs are rejected by HTTP, threaded, and Telegram surfaces.

The default `vikram` agent can act as an orchestrator: it sees available
subagents and can call `delegate_to_agent` with a self-contained prompt when a
specialized agent should do the work. In the interactive CLI, that delegation is
shown as a normal tool call before the subagent runs.

The checked-in `coder` spec defaults to local Ollama with `qwen3.8:27b-mlx`,
which is an MLX text-only model suited to Apple silicon. The `vikram`
orchestrator defaults to local Ollama with `gemma4:26b-a4b-it-qat`. These
spec pins hold even when your config sets a different global default; switch
an agent's model with `/model` (saved for next time) or override one run with
`VIKRAM_MODEL_PROVIDER`/`VIKRAM_MODEL`.

## MCP servers and skills

Agents can be extended in two declarative ways, both configured in
`spec/<agent>/agent.toml`. See [docs/mcp_and_skills.md](docs/mcp_and_skills.md)
for the full reference.

- **MCP servers** add external tools. Each `[[mcp_servers]]` entry becomes a
  Pydantic AI MCP toolset that Vikram attaches to the agent runtime.
  Secrets are referenced as `${ENV_VAR}` so specs stay safe to commit:

  ```toml
  [[mcp_servers]]
  name = "github"
  transport = "stdio"            # stdio | http | sse
  command = "npx"
  args = ["-y", "@modelcontextprotocol/server-github"]
  env = { GITHUB_PERSONAL_ACCESS_TOKEN = "${GITHUB_TOKEN}" }
  ```

- **Skills** are folders of expert instructions (`SKILL.md` with `name` and
  `description` frontmatter) under `spec/<agent>/skills/` or
  `spec/shared/skills/`. Only each skill's name and description load up front;
  the agent reads the full body on demand through the `load_skill` tool:

  ```toml
  skills = ["skills/conventional-commits"]   # relative to the agent dir
  shared_skills = ["skills/web-research"]     # relative to spec/shared
  ```

## Hooks

Hooks run your own code at agent lifecycle events to observe, augment, or block
what the agent does. They are declared per agent in `spec/<agent>/agent.toml`
under `[[hooks]]` and apply on every surface. See [docs/hooks.md](docs/hooks.md)
for the full reference.

- **Events**: `PreToolUse` and `PostToolUse` (wrap every built-in and MCP tool
  call; can block), `UserPromptSubmit` (inject context or block a run), and
  `Stop` (advisory, for notifications/logging).
- **Transports**: a `command` handler gets the event payload as JSON on stdin
  and blocks with exit code `2`; a `python` handler is a `module:function`
  callable run in-process. Secrets are referenced as `${ENV_VAR}`.

  ```toml
  [[hooks]]
  event = "PreToolUse"
  matcher = "run_command"          # glob on the tool name (default "*")
  transport = "command"
  command = "./hooks/guard.sh"     # exit 2 (stderr = reason) blocks the call

  [[hooks]]
  event = "Stop"
  transport = "python"
  entrypoint = "myhooks.notify:on_stop"   # "module:function"
  ```

## HTTP API

```bash
uv run vikram-api
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/chat --json '{"prompt":"say pong"}'
```

Endpoints:

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/healthz` | Liveness check |
| `POST` | `/chat` | Stateless one-shot run |
| `POST` | `/threads/{interface}/{thread}/messages` | Queue a durable threaded run |
| `GET` | `/events/{workflow_id}` | Read DBOS workflow status |
| `POST` | `/telegram/webhook` | Default Telegram bot webhook |
| `POST` | `/telegram/{bot_name}/webhook` | Named Telegram bot webhook |

Thread history and Telegram dedupe state default to `.vikram/vikram.sqlite3`.
DBOS workflow state defaults to `.vikram/dbos.sqlite3`.

## Telegram

`spec/telegram.toml` declares the default `vikram` bot and resolves secrets from
environment variables:

```env
VIKRAM_TELEGRAM_BOT_TOKEN=
VIKRAM_TELEGRAM_WEBHOOK_SECRET=
VIKRAM_TELEGRAM_ALLOWED_CHAT_IDS=123456789,-1001234567890
VIKRAM_TELEGRAM_BOT_USERNAME=VikramBot
```

Register a webhook with:

```bash
uv run python -m vikram.local_webhook https://example.ngrok-free.app
```

## Install

### Zero-Auth Install (Recommended for first-time setup)

Install Vikram on a brand-new machine without cloning or authenticating via GitHub CLI:

```bash
curl -LsSf https://raw.githubusercontent.com/raniendu/vikram/main/install.sh | bash
```

This single command downloads the installer, installs `uv` if missing, fetches the Vikram source archive, and configures your environment.

### From a git checkout or with `gh`

On another machine after authentication with GitHub CLI:

```bash
VIKRAM_INSTALL_DIR="$HOME/.local/share/vikram" bash install.sh
```

The installer offers to run the `vikram configure` wizard and writes provider
settings to `~/.config/vikram/config.toml`. Re-running the installer or
`vikram update` never overwrites that file, and re-running the wizard merges
into it. Install metadata lands in `~/.config/vikram/install.toml` so
`vikram update` can fast-forward and reinstall the `uv tool`.

## Docker

```bash
docker compose -f compose.example.yml --env-file .env up --build
curl http://localhost:8000/healthz
```

## Development

```bash
uv sync --locked
uv run pytest
uv run pre-commit run --all-files
docker compose -f compose.example.yml config
```

Default tests are offline and deterministic. Live model, web search, Telegram,
and tracing flows require explicit environment configuration.
