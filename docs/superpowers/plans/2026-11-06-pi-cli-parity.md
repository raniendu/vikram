# Pi CLI Parity Implementation Plan

> **For agentic workers:** Implement the approved design in independently tested
> subsystems, then integrate and review the complete diff. No commits or pushes.

**Goal:** Deliver Pi-style terminal and coding workflows with local-default
inference, OpenRouter, shared desktop state and Python extensibility.

**Architecture:** One Python session controller and shared model/tool services;
terminal and native macOS adapters. Extend the existing SQLite chat library and
private stdio protocol. Retain prompt_toolkit/Rich and the isolated Smol VM.

**Tech Stack:** Python >=3.13, Pydantic AI, prompt_toolkit 3, Rich, SQLite, SwiftUI.

**Spec:** ../specs/2026-11-06-pi-cli-parity-design.md

## Global Constraints

- The approved spec supersedes previous local-only/in-memory/no-host-tool rules.
- Preserve existing app, browser, schedules, CLI compatibility and offline evals.
- Do not commit, push, publish, run live evaluations, or touch real chat state.
- Keep credentials and thinking traces out of logs and CLI persistence.
- Keep at least 20 GiB free. Model Python composition stays in the isolated VM.

## Review Focus

- Concurrent CLI/app access must never interrupt another owner's active reply.
- Cancellation after side effects must never replay those effects implicitly.
- Untrusted output cannot emit terminal controls or bypass approval via tools.
- Missing models/credentials and failed summaries preserve drafts and history.
- Frozen installation must not rely on system Python for extension workers.

## Tasks and interfaces

### 1. Providers
- [x] Add shared provider model IDs/catalog/credentials and native OpenRouter.
- [x] Keep existing Ollama interfaces and local-only validation for schedules.
- [x] Test metadata, thinking, explicit cloud selection, private failures.

### 2. Shared sessions
- [x] Add shared Session controller and extend Store with ownership and branches.
- [x] Add persistence/resume/export/import/attachments/compaction/queues.
- [x] Test migration, concurrent ownership, interrupted state and trace privacy.

### 3. Terminal and CLI
- [x] Add full screen app, command registry, model/settings/session pickers.
- [x] Preserve regular/plain paths; add attachment/edit/copy/queue controls.
- [x] Route CLI JSON/RPC/stdin through the shared controller; test input streams.

### 4. Coding and project resources
- [x] Add trust/settings, guarded coding tools and shared approval dispatch.
- [x] Load instructions, skills/templates and keybindings only with authority.
- [x] Test exact edit conflicts, path policies, shell cancellation and plan mode.

### 5. MCP and extensions
- [x] Add lifecycle, OAuth, resource/tool discovery and native extension workers.
- [x] Add explicit package management and versioned SDK/declarative UI.
- [x] Test fake servers, worker failures and atomic package activation.

### 6. VM composition
- [x] Prove concurrent guest/control operations; implement bounded file broker.
- [x] Apply shared policies/budgets, deduplicate and propagate cancellation.
- [x] Test approvals, recursion refusal, deadlines and completed side effects.

### 7. Desktop and distribution
- [x] Add capability-aware controls and shared runtime integrations.
- [x] Bundle pinned extension Python, notices and disposable smoke checks.
- [x] Update docs/AGENTS and validate Python, Swift, startup, formatters, one
  local prompt, packaged installation; fresh review of the complete diff.

## Execution record

- 2026-11-06: Clean checkout, branch codex/pi-cli-parity, 128 GiB available.
- Ruling: work directly on a local feature branch, preserving uncommitted edits;
  no worktree/commit/publish is needed for this authorized workspace change.
- Ruling: independent provider, session-storage, and coding modules may be
  delegated with non-overlapping file ownership; integration remains centralized.

- Implemented providers, shared sessions/tree/queues/compaction, terminal controls,
  host coding/resources, MCP/OAuth, native extensions, VM composition and native
  workbench controls. Independent review findings fixed with focused regressions.
- Verified real VM file-broker concurrency and cleanup, bundled extension Python
  checksum/licenses/source inventory and disposable extension upgrade survival.
- Local Ollama discovery and one short `gemma4:12b` prompt passed; no live cloud
  calls or evaluations were run.
- Final validation: 420 Python tests passed, including the real VM and loopback
  OAuth tests; 33 Swift tests in 11 suites passed. CLI/eval startup, version
  consistency, uv lock check, Black, isort and git diff whitespace checks passed.
- Native Workbench workspace/integration/provider controls inspected in a
  disposable library; cloud disclosure and masked login rendered correctly.
- Built and signed the local app plus ZIP/DMG installers. Disposable packaged
  install/reinstall passed CLI startup/version/update help, PTY input/plan/quit,
  VM, browser, scheduler runner, desktop service, interpreter/worker and notices
  checks. Runtime links are dereferenced only after extraction validates targets,
  retaining the installer's existing regular-file-only rule.
- Independent review's important findings resolved and regression tested:
  draft recovery, startup/stop liveness, uncertain side effects through compaction,
  scoped instructions before mutations, secret environments, approval parameter
  previews, isolated imports, explicit cloud selection and integration recovery.
- Owner installation, real chats, launchd, releases and Git history were not
  changed. Artifacts remain local under dist/installers; no commits or publishing.

## Model selection follow-up (2026-11-06)

- Owner reported the missing-model notice appeared without a selector and requested
  remembered machine defaults plus a shared app-level default control.
- Missing models now open a searchable picker automatically, including an offline
  refresh action. Ctrl+L, F2 and /model reopen it without losing the draft.
- Explicit picker choices update user settings for new CLI and desktop sessions.
  Settings → Default Model (also Workbench → Models and terminal /settings or
  /default-model) changes the shared default independently of the open chat.
- Explicit CLI/environment selections and restored session models retain precedence;
  trusted project overrides remain scoped. Saved cloud defaults require the explicit
  OpenRouter disclosure and never fall back to a different provider.
- Regression validation: 429 Python checks and 33 Swift checks passed. The native
  Settings picker was exercised with a disposable library and saved the shared
  model setting. Rebuilt app/ZIP/DMG and disposable install/reinstall checks passed,
  including the frozen terminal auto-picker. CLI startup, Black/isort and whitespace
  checks passed. The owner installation and actual default setting were not changed.
