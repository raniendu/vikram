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

For direct app installs, `vikram configure` (alias: `vikram setup`) writes
multi-provider model config to `~/.config/vikram/config.toml` and is safe to
re-run — it merges instead of overwriting. Deployment environments can use env
vars instead; provider and model must be set explicitly.

For local Ollama:

```env
VIKRAM_MODEL_PROVIDER=ollama
VIKRAM_MODEL=<model-tag>
OLLAMA_BASE_URL=http://localhost:11434/v1
```

For hosted providers, set the provider id, the model, and that provider's key:

```env
# Anthropic Claude
VIKRAM_MODEL_PROVIDER=anthropic
VIKRAM_MODEL=claude-sonnet-5
ANTHROPIC_API_KEY=...

# Google Gemini
VIKRAM_MODEL_PROVIDER=gemini
VIKRAM_MODEL=gemini-2.5-flash
GEMINI_API_KEY=...

# OpenAI
VIKRAM_MODEL_PROVIDER=openai
VIKRAM_MODEL=gpt-5-mini
OPENAI_API_KEY=...

# DigitalOcean serverless inference
VIKRAM_MODEL_PROVIDER=digitalocean
VIKRAM_MODEL=llama3.3-70b-instruct
DIGITALOCEAN_ACCESS_TOKEN=...

# Ollama Cloud
VIKRAM_MODEL_PROVIDER=ollama-cloud
VIKRAM_MODEL=gpt-oss:120b
OLLAMA_API_KEY=...

# Any other OpenAI-compatible endpoint
VIKRAM_MODEL_PROVIDER=openai-compatible
VIKRAM_MODEL=<model-id>
VIKRAM_OPENAI_COMPAT_API_KEY=...
VIKRAM_OPENAI_COMPAT_BASE_URL=https://api.example.com/v1
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

OpenLIT/OpenTelemetry tracing is opt-in:

```env
VIKRAM_OBSERVABILITY_ENABLED=true
VIKRAM_OTLP_ENDPOINT=http://localhost:4318
```

Message-content capture is disabled by default and forced off in production.
