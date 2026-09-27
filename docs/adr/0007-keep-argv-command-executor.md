# 0007. Keep the argv-only command executor; strip secrets from its environment

- **Status:** Accepted
- **Date:** 2026-09-26
- **PR:** raniendu/vikram#42 (WS3)

## Context

The `coder` agent runs commands with `run_command`/`inspect_command`. Vikram
executes an **argument list with no shell**, and its command policy (read-only
classification, a deny backstop, approval for anything else) is written for
that. The harness `Shell` capability runs everything through `/bin/sh -c`.

Commands also inherited the whole environment, including provider API keys.

## Options considered

| Option | For | Against |
|---|---|---|
| Harness `Shell` | Pipes, redirects, globbing | `git status; rm -rf ~`, `$(curl ...)` and `> file` become live; a deny list on raw shell text is, in the harness's own words, "best-effort guardrails, not security boundaries" |
| **Keep argv executor, adopt the harness's secret patterns** | Policy stays sound; secrets stop leaking to child processes | No shell features for the agent |

## Decision

Keep the argv-only executor. Adopt the harness's `LLM_API_KEY_ENV_PATTERNS`:
commands run without provider API keys (`ANTHROPIC_*`, `OPENAI_*`,
`GEMINI_*`, `GOOGLE_*`, ...), `VIKRAM_*` settings (including the Telegram bot
token) and Vikram's own service keys (`OLLAMA_API_KEY`, `PARALLEL_API_KEY`,
`SARVAM_API_KEY`, `DIGITALOCEAN_ACCESS_TOKEN`). `GITHUB_TOKEN`/`GH_TOKEN` are
kept, because `gh pr create` is a documented `coder` workflow.

## Consequences

- A command the model runs can no longer read or send out Vikram's API keys.
- The agent still can't use pipes or redirects; it runs separate commands.

## Revisit when

The harness offers argv execution, or Vikram runs commands inside an OS sandbox
(container, Modal), where a shell is safe.
