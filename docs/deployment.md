# Deployment

Vikram can run directly with `uv`, as a `uv tool`, or in Docker.

## Direct App

```bash
uv sync --locked
uv run vikram configure
uv run vikram-api
curl http://127.0.0.1:8000/healthz
```

## Docker

```bash
docker compose -f compose.example.yml --env-file .env up --build
curl http://localhost:8000/healthz
```

Mount `/app/.vikram` if you want to preserve thread history and DBOS workflow
state across container restarts.

## Required Runtime Env

For direct app installs, `vikram configure` writes local model config to
`~/.config/vikram/config.toml`. Deployment environments can use env vars
instead; provider and model must be set explicitly.

For local Ollama:

```env
VIKRAM_MODEL_PROVIDER=ollama
VIKRAM_MODEL=<model-tag>
OLLAMA_BASE_URL=http://localhost:11434/v1
```

For a hosted OpenAI-compatible endpoint:

```env
VIKRAM_MODEL_PROVIDER=openai-compatible
VIKRAM_OPENAI_COMPAT_API_KEY=...
VIKRAM_OPENAI_COMPAT_BASE_URL=https://api.openai.com/v1
VIKRAM_MODEL=gpt-4.1-mini
```

For Telegram:

```env
VIKRAM_PUBLIC_BASE_URL=https://example.ngrok-free.app
VIKRAM_TELEGRAM_BOT_TOKEN=...
VIKRAM_TELEGRAM_WEBHOOK_SECRET=...
VIKRAM_TELEGRAM_ALLOWED_CHAT_IDS=123456789
VIKRAM_TELEGRAM_BOT_USERNAME=VikramBot
```

## Logs And Traces

Logs are structured JSON on stdout. Chat and thread IDs are hashed and prompt
content is not logged by default.

OpenLIT tracing is opt-in:

```env
VIKRAM_OBSERVABILITY_ENABLED=true
VIKRAM_OTLP_ENDPOINT=http://localhost:4318
```

Message-content capture is disabled by default and forced off in production.
