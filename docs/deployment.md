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

## Health And Readiness

Two probes, with different jobs:

- `GET /healthz` — liveness. Always `{"status": "ok"}` when the process is
  serving; it touches no dependencies. Use it for container liveness checks.
- `GET /readyz` — readiness. Exercises model config, the thread store, and the
  default agent, and answers `503` with a per-check breakdown when any is
  broken. Use it as the deploy gate and load-balancer readiness check.

```bash
curl -s http://localhost:8000/readyz | jq
```

```json
{
  "status": "ready",
  "version": "0.1.0",
  "environment": "local",
  "default_agent": "vikram",
  "checks": {
    "model_config": "ok",
    "thread_store": "ok",
    "default_agent": "ok"
  }
}
```

## Logs And Traces

Logs are structured JSON, one object per line. Chat and thread IDs are hashed,
prompt and response bodies are logged only as lengths, and command execution is
logged by executable name and argument count rather than the command string, so
credentials passed as flags never reach the logs.

Server surfaces log to stdout. The CLI and ACP log to **stderr** instead,
because their stdout carries the product: chat output and `exec --json` payloads
for the CLI, and the JSON-RPC stream for ACP. The CLI also stays at `WARNING`
unless `VIKRAM_LOG_LEVEL` is set explicitly, so the interactive session is not
interleaved with info-level logging.

Every HTTP request gets an `x-request-id` (generated, or taken from an inbound
header of the same name) that is echoed back in the response and bound to every
log line emitted while handling that request. Quote it when reporting an issue.

OpenLIT/OpenTelemetry tracing is opt-in and applies to every surface — HTTP,
CLI, and ACP:

```env
VIKRAM_OBSERVABILITY_ENABLED=true
VIKRAM_OTLP_ENDPOINT=http://localhost:4318
```

When enabled, an inbound HTTP request and the DBOS workflow that eventually
answers it share a single trace: the W3C `traceparent` rides along as a
CloudEvents distributed-tracing attribute across the queue boundary. Log lines
emitted inside a span also carry `trace_id` and `span_id`, so logs and traces
join up.

Message-content capture is disabled by default and forced off in production.
