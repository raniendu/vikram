# 0009. Prompt-injection defence in report mode; secret redaction on; memory opt-in

- **Status:** Proposed
- **Date:** 2026-09-26
- **PR:** raniendu/vikram#44 (WS7), landing on `main` via raniendu/vikram#47

## Context

Tool results (web pages, files, MCP output) can contain planted instructions
("ignore previous instructions and ..."). Replies can echo secrets that
appeared in a tool result. The harness offers a local `PromptInjectionDefender`,
secret-redacting guardrails, and a persistent `Memory` notebook.

## Decision

| Capability | Setting | Why |
|---|---|---|
| `prompt_injection` | **report** mode, both agents | Logs `prompt_injection_detected` (tool and risk level only) without changing answers, so we can measure false positives before blocking anything |
| `guardrails.redact_secrets` | `["output"]` for `vikram` | Its replies go to Telegram and HTTP callers; known key formats are redacted |
| `memory` | supported, **off** | It stores whatever the model chooses to remember; turning it on is a deliberate per-agent choice |

Detection is local pattern matching; no content is sent anywhere. Memory, when
on, lives in local SQLite, keyed per conversation by a hash.

## Consequences

- Injection attempts become visible in logs.
- Nothing is blocked yet, so a real injection still reaches the model.
- Redaction can occasionally mask a harmless string that looks like a key.

## Revisit when

Report-mode logs show a low false-positive rate: switch `prompt_injection` to
`block` for the network-facing agent.
