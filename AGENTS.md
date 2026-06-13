# Repository Guidelines

## Project Structure

`vikram/` contains the package code. Agent specs live under `spec/<agent>/` and
shared policy/context lives under `spec/shared/`. Tests live in `tests/`. Runtime
state belongs under `.vikram/` and must not be committed.

Key modules:
- `agent.py`: builds Pydantic AI agents from specs, tools, MCP servers, skills, and hooks.
- `mcp.py`: declarative `[[mcp_servers]]` specs and MCP toolset construction.
- `skills.py`: Agent Skills discovery and the `load_skill` progressive-disclosure tool.
- `hooks.py`: declarative `[[hooks]]` specs, the `HookToolset` (Pre/PostToolUse) and `HookedAgent` (UserPromptSubmit/Stop).
- `cli.py`: `vikram` command, including interactive and one-shot modes.
- `acp.py`: `vikram-acp` editor-facing Agent Client Protocol adapter.
- `api.py`: FastAPI app for `/chat`, threaded events, Telegram webhooks, and health.
- `gateway.py` and `dbos_gateway.py`: SQLite thread history and DBOS queues.
- `telegram.py` and `telegram_config.py`: Telegram parsing, allowlists, commands, and delivery.
- `tools.py` and `command_policy.py`: web search plus local coding tools and command policy.
- `settings.py`: environment-driven settings and model provider construction.

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

## Testing

Tests use `pytest` with `pytest-asyncio` in auto mode. Keep default tests offline
and deterministic. Gate live model calls, web search, Telegram, or tracing behind
explicit environment variables. For threaded/API tests, patch
`vikram.api._get_dispatcher` rather than booting real DBOS workflows.

## Security

Do not commit secrets, populated `.env` files, Telegram tokens, webhook secrets,
chat IDs from real deployments, private keys, or local state. Logs and tests
should avoid raw prompt text, bot tokens, and private identifiers. The `coder`
agent must remain CLI-only.
