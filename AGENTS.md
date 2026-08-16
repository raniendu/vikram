# Repository Guidelines

## Project Structure

`vikram/` contains the package code. Agent specs live under `spec/<agent>/` and
shared policy/context lives under `spec/shared/`. Tests live in `tests/`. Runtime
state belongs under `.vikram/` and must not be committed.

Key modules:
- `agent.py`: builds Pydantic AI agents from specs, tools, MCP servers, skills, and hooks.
- `mcp.py`: declarative `[[mcp_servers]]` specs and MCP toolset construction.
- `skills.py`: Agent Skills discovery and the `load_skill` progressive-disclosure tool.
- `hooks.py`: declarative `[[hooks]]` specs compiled into a Pydantic AI wrapper
  toolset plus prompt/stop callbacks.
- `cli.py`: `vikram` command, including interactive and one-shot modes.
- `acp.py`: `vikram-acp` editor-facing Agent Client Protocol adapter.
- `api.py`: FastAPI app for `/chat`, threaded events, Telegram webhooks, and health.
- `gateway.py` and `dbos_gateway.py`: SQLite thread history and DBOS queues.
- `telegram.py` and `telegram_config.py`: Telegram parsing, allowlists, commands, and delivery.
- `tools.py` and `command_policy.py`: web search plus local coding tools and command policy.
- `settings.py`: environment-driven settings and model provider construction.
- `logging.py` and `observability.py`: structlog configuration, redaction
  helpers, and OpenTelemetry tracer/propagation helpers.

## Commands

- `uv sync --locked`: install dependencies.
- `uv run vikram`: start the default interactive CLI agent.
- `uv run vikram --agent coder`: start the local CLI-only coding agent.
- `uv run vikram --once --prompt "..." --json`: run one prompt and emit JSON.
- `uv run vikram-api`: serve FastAPI on `http://127.0.0.1:8000`.
- `uv run vikram-acp --agent coder`: start ACP over stdio.
- `uv run pytest`: run the offline test suite.
- `uv run pre-commit run --all-files`: run Black and isort.
- `docker compose -f compose.example.yml config`: validate the example Compose file.

## Style

Use Python 3.13+ features with type hints on public boundaries. Formatting is
Black with an 88-character line length; imports are sorted by isort using the
Black profile. Keep tool names stable because specs reference
`vikram.tools.TOOL_REGISTRY`.

## Logging And Observability

Log through `vikram.logging.get_logger`, never stdlib `logging` or `print`, and
use structlog's keyword style (`log.info("event_name", key=value)`) with
snake_case event names in the past tense where an action completed. `event` is
reserved by structlog — name a field `hook_event` or similar instead.

Log identifiers and sizes, never content: use `prompt_length` over the prompt,
`thread_hash`/`chat_hash` over raw ids, and an executable name plus argument
count over a full command string. Never log tokens, secrets, API keys, MCP
`url`/`env` values, or anything derived from them.

Do not swallow an exception silently. Either log it (`log.exception(...)`) or
narrow the `except` clause so genuine bugs still surface.

Front-ends whose stdout is the product — the CLI and ACP — must pass
`stream=sys.stderr` to `configure_logging`.

## Testing

Tests use `pytest` with `pytest-asyncio` in auto mode. Keep default tests offline
and deterministic. Gate live model calls, web search, Telegram, or tracing behind
explicit environment variables. For threaded/API tests, patch
`vikram.api._get_dispatcher` rather than booting real DBOS workflows.

To assert on logs, use the helpers in `tests/conftest.py`: the `log_events`
fixture (plus `find_log_event`) for unit-level assertions, and
`captured_json_logs()` when the assertion depends on the configured processor
chain, such as request-id correlation. `caplog` does not work for structlog
events unless `configure_logging` has already run.

## Security

Do not commit secrets, populated `.env` files, Telegram tokens, webhook secrets,
chat IDs from real deployments, private keys, or local state. Logs and tests
should avoid raw prompt text, bot tokens, and private identifiers. The `coder`
agent must remain CLI-only.
