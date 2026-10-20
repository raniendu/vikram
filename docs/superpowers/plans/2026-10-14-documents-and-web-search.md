# Documents and Web Search Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task by task. Steps use checkbox syntax for tracking.

**Goal:** Add local document attachments and free automatic Parallel search with identical CLI/app model-tool execution.

**Architecture:** One shared conversation runner owns capability validation, Pydantic AI streaming/tool execution, bounded web access, and canonical history. CLI and desktop adapt its inputs/events. Desktop adds durable documents and source records.

**Tech Stack:** Python 3.13, Pydantic AI, official MCP Python SDK, pypdf, python-docx, openpyxl, React/TypeScript, Tauri 2.

**Spec:** [Approved design](../specs/2026-10-14-documents-and-web-search-design.md).

## Global constraints

- Local Ollama inference only; Parallel anonymous MCP only, no paid fallback.
- Keep 20 GiB disk reserve; four attachments, 10 MiB each, 20,000 extracted characters each.
- Three serial remote calls, five model requests, six sources/20,000 excerpt characters per reply; 30 seconds per remote call, 60 seconds total, 1 MiB response cap.
- Preserve user data and evaluation history. Evaluations explicitly use web Off.
- No commits, pushes, PRs, or changes to unrelated workloads.
- Execute in the existing `codex/local-benchmarks` checkout: the approved base is uncommitted here and must be preserved.

## Review focus

- A model emits text before calling a tool: keep the loop active to its actual final answer (Task 1).
- Malformed or overlarge remote results cannot bypass source/transport limits (Task 1).
- Existing libraries migrate atomically without enabling remote access (Task 3).
- An import running during generation cannot prevent Stop or shutdown (Task 3).
- A lost IPC reply or model switch cannot route sources to another attempt (Task 4).

## Task 1: Shared conversation and bounded web tools

**Files:** `vikram/conversation.py`, `vikram/context.py`, `vikram/models.py`, `vikram/web.py`, `vikram/agent.py`, `tests/test_conversation.py`, `tests/test_web.py`, `pyproject.toml`, `uv.lock`.

**Interfaces:** `prepare_model(models, name, thinking, web)` validates run settings; `stream_reply(model, thinking, history, prompt, *, web, session_id)` yields dictionaries containing `text`, `actual_model`, `phase`, `sources`, `searches`. `context_messages(turns)` builds history from completed canonical turns. `WebSession.call(name, arguments)` validates, caps, normalizes and emits search events; MCP connects lazily.

- [x] Write and run failing tests for real scripted model/tool loops, Off/no-network, capability/limit behavior, canonical source history and malicious/oversized results.
- [x] Implement shared capability discovery, common instructions, lazy MCP transport, normalized sources, and Pydantic AI iteration with no hidden retries.
- [x] Run focused tests; expected: all pass without network or Ollama.

## Task 2: CLI and desktop adapters

**Files:** `vikram/cli.py`, `vikram/desktop/inference.py`, `vikram/desktop/models.py`, `vikram/desktop/history.py`, `vikram/desktop/runtime.py`, `vikram/evals/cli.py`, `tests/test_cli_parity.py`, existing runtime tests.

**Interfaces:** Both adapters call Task 1's runner with the same run options and canonical history. CLI `--web auto|off`, `/web`, and `--thinking`; evaluation factory explicitly selects Off.

- [x] Write and run failing adapter parity tests comparing actual model requests and tool evidence, including follow-up turns and failure paths.
- [x] Replace duplicated execution with adapters; preserve CLI model precedence and desktop routing/cancellation.
- [x] Run parity, runtime, CLI help and evaluation help checks; expected: parity tests pass and no model calls from help.

## Task 3: Document extraction and durable migration

**Files:** `vikram/documents.py`, `vikram/document_worker.py`, `vikram/desktop/store.py`, `vikram/desktop/protocol.py`, `vikram/desktop/runtime.py`, `vikram/desktop/__main__.py`, `scripts/desktop_sidecar.py`, `tests/test_documents.py`, store/protocol tests.

**Interfaces:** `extract_document(name, data)` returns kind/media/text/notice metadata; `extract_in_worker(name, data)` enforces 15-second process lifetime. `Store.import_attachment` persists a validated extraction. Schema 2 adds extraction fields, web preference and attempt sources/searches; schema 3 adds checkpoint revisions for ordered recovery. Generic import/preview/remove IPC retains image compatibility.

- [x] Write and run failing tests for each format, partial content, malformed/encrypted/archive files, resource limits, migration and Stop during import.
- [x] Add document dependencies and bounded extraction worker; migrate SQLite transactionally, extending request snapshots and history.
- [x] Run document/store/protocol/runtime tests; expected: old image behavior and new documents work offline, original files untouched.

## Task 4: Tauri controls and source/document views

**Files:** `apps/desktop/src/{types,state,useChats,App}.ts*`, components, styles, Rust `main.rs`, frontend tests.

**Interfaces:** Attachment kind/extraction preview; per-chat web mode; attempt-scoped sources/searches events. Native file grants stay bounded to selected/dropped files.

- [x] Add failing UI recovery tests for source event routing, web settings, and document sending with a text-only model.
- [x] Extend picker/drop/paste, previews, Auto/Off and status controls, sources and notices using the current theme.
- [x] Run frontend suite/build and native tests; expected: all pass, restricted renderer networking unchanged.

## Task 5: Review, live checks and packaging

**Files:** README, AGENTS, build script, plan validation record.

- [x] Run Python suite, frontend suite/build, Rust tests, formatters and CLI/eval startup checks.
- [x] Request one fresh review of the implementation and fix material findings with regression tests.
- [x] Verify anonymous MCP with public queries and short local model tool calls through both interfaces; do not run live evaluations.
- [x] Build the Apple Silicon app, verify signature/sidecar startup and native document/search UI with temporary fixtures; keep 20 GiB free.
- [x] Record actual validation and any remaining limitations; leave changes uncommitted.

## Execution record

- Owner explicitly approved the updated spec and instructed implementation. Proceed inline without another authorization round; a fresh reviewer will check the completed change.
- Existing working changes are the approved chat app. Preserve them; no clean checkout or commit will replace them.
- Pre-flight: Task 1 event schema feeds Tasks 2/4 and Task 3 attempt persistence; all use `sources` and `searches`. Task 3 document metadata feeds Task 4 attachment view. No interface conflict found.

- Tasks 1–4: shared runner, adapters, document storage/migration, and UI implemented. Python suite: 42 passing; frontend suite: 16 passing; TypeScript/Vite build passes. Live anonymous MCP returned sources without authentication.
- Parser and terminal history code are shared where applicable; desktop stores normalized evidence and CLI retains the same evidence in memory.
- Validation caught and fixed a sidecar entry-point indentation error; an actual subprocess test now verifies Stop is processed while a document import is pending.

## Final validation — October 14, 2026

- Python: **50 tests passed**. Coverage includes real scripted Pydantic AI tool loops, CLI/app wire-request parity across successful and failed search turns and follow-up history, metadata changes, cancellation of an in-flight remote operation, budget exhaustion followed by a tool-free answer, compressed/oversized responses, extraction bounds, v1 migration, source persistence and failed checkpoints.
- Frontend: **19 tests passed**; TypeScript/Vite production build passed. Regression coverage includes shorter replacement answers with source evidence, stale events, document-only messages on a text-only model, and search/fetch status after intermediate text.
- Rust: **1 lifecycle test passed**. Black, isort, whitespace checks, CLI help and evaluation CLI help passed. No live evaluations ran.
- Independent reviewer found six issues: unsaved replacement recovery, compressed-response cap bypass, thinking fallback divergence, web status precedence, empty documents accepted through synthetic labels, and missing document instructions in Off. All were fixed; the reviewer rechecked and reported no remaining actionable issues in those fixes.
- Apple Silicon app rebuilt and signed locally. Deep/strict signature verification and bundled sidecar startup passed. The initial startup check inside the shell sandbox was blocked by a macOS semaphore restriction; the same binary passed outside that sandbox.
- Packaged service imported generated PDF, DOCX, TXT, CSV and XLSX fixtures into an isolated temporary library. Each returned extracted document text and shut down cleanly.
- Native UI: file picker, extraction progress, document preview, thinking setting, Web Off/Auto, final answers, activity details and source links verified. With `gemma4:26b-a4b-it-qat` thinking Off, the attached text produced “Sapphire” with Web Off; Web Auto performed one anonymous search and displayed the official Ollama tool-calling URL plus six retrieved sources. The CLI performed the corresponding public search successfully as well.
- After quitting the test app, its SQLite library retained the document, completed replies, captured modes and six sources. Reopened the normal app: the owner's two existing chats remained visible and migrated with Web Off.
- Approximately **67.9 GiB free** after building and testing; the 20 GiB reserve remains intact. Changes are uncommitted.

### Practical limits

- Live tool behavior was checked with Gemma 4, not every installed model. Automated tests cover provider failures and cancellation; no outage or rate limit was induced against the live service.
- The native picker and preview were exercised manually for text, with every parser format checked through the packaged service. Other file-drop/paste combinations rely on the shared attachment path and automated checks.
- Existing scope limits remain: no OCR, legacy Office files, audio/video, indexing, hosted inference or arbitrary MCP servers. The local development app is ad-hoc signed, not notarized for distribution.

## Follow-up: conversation deletion and current time

The owner requested visible conversation deletion and a system timestamp in the
shared model context. The existing deletion action was hidden in the header's
three-dot menu; the sidebar now exposes a trash button beside every chat. The
confirmation captures the target ID/title, and deleting another chat preserves
the selected chat. A running chat must still be stopped before deletion.

The agent now supplies fresh local and UTC timestamps, local weekday and timezone
on each model request through dynamic system instructions. This applies equally
to CLI and app, including Web Off and reused sessions. A clock regression test
covers crossing midnight; the existing adapter parity tests now freeze the same
system clock while comparing requests.

Validation: 51 Python tests, 24 frontend tests and one Rust lifecycle test passed.
TypeScript/Vite, Black, isort, CLI/evaluation help, bundle signing and frozen
startup checks passed. A live CLI check with Gemma 4 correctly returned the Mac's
local date and 9:32 PM PDT while Web Off. The rebuilt native UI showed the trash
buttons, opened the correct other-chat confirmation, and preserved both existing
chats when canceled. No owner conversations were deleted. Free space after the
build was 65.2 GiB, above the 20 GiB reserve. Changes remain uncommitted.

The additional live clock check through the packaged service using
`ornith-1.5:35b` did not complete within the 120-second smoke-test limit. Its
temporary service was stopped and its isolated library removed. This does not
establish Ornith's answer behavior; the live successful answer check was Gemma 4,
while automated tests verify current clock injection in both model-request paths.
