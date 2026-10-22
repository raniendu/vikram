# Local chat implementation plan

> **For agentic workers:** Use superpowers:executing-plans inline, with a fresh final review. No commits or publishing are authorized.

**Goal:** Deliver an Apple Silicon Tauri app for persistent, local Ollama conversations.

**Architecture:** React calls a narrow Rust bridge. A bundled Python process owns SQLite, images, metadata and generation through the existing shared agent. Version 1 JSON-lines stdio carries correlated commands and events; no application HTTP server.

**Tech Stack:** Python 3.13, Pydantic AI 2.54, SQLite, Pillow, React/TypeScript/Vite, Tauri 2, PyInstaller.

**Spec:** [Approved local chat design](../specs/2026-10-14-local-chat-design.md)

## Global constraints

- Preserve CLI/evaluation behavior, metrics and all unrelated local state.
- No hosted inference, analytics, generated-code execution or arbitrary renderer filesystem access.
- One active generation, exact per-chat model and metadata-supported thinking controls.
- PNG/JPEG/WebP: four images, 10 MiB each, 24 MP decoded; independent app-owned copies.
- Use the website's exact light/dark tokens, bundled Inter and Space Grotesk, 3px radius.
- Keep 20 GiB free; no model downloads, automatic evaluations, commits, pushes or PRs.

## Review focus

1. A renamed cloud model must not bypass local model admission.
2. A stopped stream that emits late data must never corrupt another chat.
3. A crash during import or streaming must recover without duplicate user messages.
4. Changing chat/model while drafting or generating must preserve captured settings and drafts.
5. Disk errors and sidecar exit must preserve visible unsaved content and allow copying.

## Task 1: Durable records and safe image ownership

**Files:** `vikram/desktop/store.py`, `attachments.py`, `tests/test_desktop_store.py`.
**Interfaces:** `Store(Path)` provides snapshots, CRUD, draft import and attempt transitions; attachment IDs never accept paths from renderer.

- [x] Write and observe failing tests for persistence, retry eligibility, folder uniqueness, interrupted recovery, attachment ownership/limits and cross-chat isolation.
- [x] Implement schema versioning, transactions, private file permissions, import recovery and constrained transitions.
- [x] Run focused offline tests.

## Task 2: Model discovery and generation protocol

**Files:** `vikram/ollama.py`, `vikram/desktop/{models,history,runtime,__main__}.py`, `tests/test_desktop_runtime.py`; minimal shared thinking refactor.
**Interfaces:** versioned `{v,id,method,params}` commands, `{v,id,result|error}` replies, `{v,event,request_id,chat_id,attempt_id,...}` events.

- [x] Write failing metadata/history/cancellation tests, including cloud aliases, stale thinking values, late chunks and persistence failure.
- [x] Implement bounded local metadata calls, fail-closed model validation, shared-agent streaming, cancellation and stdio lifecycle.
- [x] Verify offline tests and original CLI/eval help.

## Task 3: Tauri and the conversation interface

**Files:** `apps/desktop/src-tauri/`, `apps/desktop/src/{App,bridge,types,styles,components/*}` and package configuration.
**Interfaces:** narrow Tauri commands relay the protocol; native selection/drop permits image bytes only; UI events reconcile by stable identifiers.

- [x] Build shell and components for sidebar, transcript, model controls, composer, dialogs and settings.
- [x] Implement persisted drafts, Markdown/code copy, retry/stop, shortcuts, native image import, light/dark/system, and recoverable errors.
- [x] Verify TS/frontend build and Rust checks; exercise UI behavior and both themes.

## Task 4: Package, validate and hand off

**Files:** `scripts/build_desktop.py`, README, AGENTS, this progress ledger.

- [x] Bundle native Python sidecar and fonts/licenses; build the Apple Silicon .app.
- [x] Run offline tests, formatters, CLI/eval startup, local text/image/thinking smoke and packaged UI tests.
- [x] Obtain fresh code review, fix significant findings, rerun affected checks.
- [x] Document exact run/build commands, checked acceptance criteria and limitations.

## Execution ledger

- Owner approved the spec/defaults and requested the build on October 14, 2026.
- Ruling: proceed inline in the existing non-default working branch; preserve current docs/AGENTS changes. No extra plan approval or commits, per the user's build authorization and repository instructions.
- Interface check: Store owns transactions; Runtime owns generation slot; bridge never touches SQLite; UI receives only records and attachment bytes. All layers use protocol version 1.
- Visual ruling: use the already approved website-derived design system as the binding visual reference. No new art direction or generated concept approval is needed.

- Tasks 1–4 implemented and packaged. No commits or publishing performed.
- Fresh independent review found recovery defects around blocked writes, uncertain sends, stale snapshots, cancellation and unsaved drafts. Each significant finding was fixed with a focused regression test. The final scoped review had no significant findings remaining.
- Desktop transport retries are disabled. A failed inference waits for an explicit Retry.
- Local packaging uses explicit ad-hoc signing with Hardened Runtime off for the bundled Python libraries. The build checks both the bundle signature and a no-inference sidecar handshake in a temporary library. Notarization/distribution signing is deferred.

## Validation record

Completed on October 14, 2026:

| Area | Evidence |
| --- | --- |
| Python runtime | 21 offline tests pass: persistence, recovery, images, metadata admission, history mapping, strict IPC, cancellation, disk failures, provenance and no automatic HTTP retries |
| React controller | 15 tests pass, including uncertain send acknowledgements, stale active state, newer disk checkpoints, edits during reconnect and unsaved-draft quit protection |
| Rust bridge | Blocked-stdin shutdown regression passes; Rust formatting/checks and release compilation pass |
| Build/style | TypeScript/Vite production build, Black, isort, Prettier and Git whitespace checks pass |
| Shared CLI/evaluations | Both help commands start; 21 existing evaluation records load without modification; no live evaluations run |
| Packaged service | Native ARM64 Python included; versioned handshake works without repository Python; bundle signature validates deeply and strictly |
| Local inference | Streamed text, actual model attribution, image recognition and explicit Thinking Off verified using the installed Gemma model |
| Public networking unavailable | Packaged service discovers eight local models and completes a reply under a temporary macOS process policy that denies public network access and allows only local Ollama; host network settings unchanged |
| Native interface | Chat creation, independent drafts, restart persistence, per-model thinking, folder creation, model switching, image picker/import, image reply, Stop and Retry verified |
| Image ownership | After moving the original PNG and restarting the signed app, its saved image still opens correctly |
| Image/text model switch | Native warning appears and prior replies retain their original model attribution; mocked history checks prove image bytes are omitted for text-only models |
| Visuals | Website-derived light and dark palettes inspected; bundled fonts and restrictive image/network CSP verified |

Additional manual coverage still useful before distributing beyond this Mac:
image paste and native drag/drop, OS-driven appearance changes, exhaustive keyboard/
VoiceOver navigation, and Developer ID signing/notarization. File-picker import
and attachment validation/ownership are covered; those additional input routes
are implemented but not yet exercised end to end on macOS.
