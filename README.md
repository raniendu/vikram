# Vikram

Vikram is a public, standalone agent runtime built on Pydantic AI. It keeps
agent behavior in versioned specs under `spec/`, exposes the same agents through
CLI, HTTP, threaded queues, Telegram webhooks, and ACP, and ships safe local
coding tools for the CLI-only `coder` agent.

## Features

- Spec-driven agents: `spec/<agent>/agent.toml` plus Markdown prompts.
- Built-in agents: `vikram` for general assistance and `coder` for local coding.
- CLI: interactive chat, one-shot prompts, JSON output, and self-update.
- ACP: editor integration for the local `coder` agent.
- HTTP: stateless `/chat`, durable `/threads/...`, `/events/...`, and health.
- Telegram: env-driven bot config, allowlist, group mention/reply routing, and
  `/reset` plus `/agent` commands.
- Tools: Parallel web search, safe file/search/edit tools, and argv-only command
  execution guarded by a declarative command policy.
- Runtime state: local SQLite for thread history and DBOS workflow state.
- Observability: structured JSON logs and optional OpenLIT/OpenTelemetry traces.

## Quick Start

```bash
uv sync
uv run vikram configure
uv run vikram --once --prompt "say pong"
```

Vikram does not ship with a default model provider or model name. Configuration
lives in `~/.config/vikram/config.toml`; environment variables and `.env` still
override that local file for development and deployment.

For local Ollama, pull a model you want to use, then run `vikram configure` and
enter that exact tag:

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

Equivalent `.env` settings for a hosted OpenAI-compatible endpoint:

```env
VIKRAM_MODEL_PROVIDER=openai-compatible
VIKRAM_OPENAI_COMPAT_API_KEY=...
VIKRAM_OPENAI_COMPAT_BASE_URL=https://api.openai.com/v1
VIKRAM_MODEL=gpt-4.1-mini
```

## CLI

```bash
uv run vikram configure
uv run vikram
uv run vikram --agent coder
uv run vikram --once --prompt "summarize this repo"
uv run vikram --once --prompt @prompt.txt --json
vikram update --check
```

The `coder` agent is CLI-only. It can read/search files, request approval for
edits, and run commands through `spec/shared/command_policy.toml`. CLI-only
specs are rejected by HTTP, threaded, and Telegram surfaces.

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

From a checkout:

```bash
bash install.sh
```

On another machine after authentication with GitHub CLI:

```bash
VIKRAM_INSTALL_DIR="$HOME/.local/share/vikram" bash install.sh
```

The installer asks for model configuration and writes it to
`~/.config/vikram/config.toml`. It records install metadata in
`~/.config/vikram/install.toml` so `vikram update` can fast-forward and
reinstall the `uv tool`.

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
