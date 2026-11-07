# Pi 1.5.0 parity for Vikram

Approved by the owner in the planning conversation on 2026-11-06, followed by
an explicit request to implement the plan. The reference is the locally
installed @earendil-works/pi-coding-agent 1.5.0.

## Decisions

Keep Python, Pydantic AI, prompt_toolkit and Rich. Default to a fullscreen TUI
with regular scrollback and plain output alternatives. Ollama is the default;
OpenRouter is the only cloud provider, selected explicitly without automatic
fallback. Use one shared CLI/macOS conversation library and one execution core.
Build CLI controls first, then complete the corresponding desktop controls.

Project trust enables host coding tools with normal user privileges, not a
sandbox. Approve outside-workspace operations, deletion/destructive Git,
credential access, package installation, publishing/remote mutations and
unclassified shell effects. Existing browser approvals remain. Plan mode permits
inspection only. The original Python sandbox remains networkless and mountless.
Scheduled runs and evaluations do not gain host tools, extensions or cloud
inference by default. No automatic publishing, commits, or releases.

## Required behavior

1. Fullscreen/regular TUI: persistent multiline editor, searchable commands and
   models, local autodiscovery, capability-aware thinking, scoped model cycling,
   settings/themes/keybindings, transcript search/copy, expandable tools/thinking,
   resize/Unicode support, streaming cancellation and cached rendering. Enter
   sends, Shift+Enter/Ctrl+J inserts newline, Escape stops, Ctrl+L picks model,
   Ctrl+T toggles thinking, Ctrl+O toggles tools.
2. Shared SQLite sessions: resume/name/search/delete, non-destructive
   tree/fork/clone, workspace metadata, per-session ownership, crash recovery,
   attachment reuse, @file completion, clipboard/drop images, external editor,
   local Markdown/HTML/versioned JSONL export/import. Branching does not undo
   files. CLI display thinking is never persisted or exported.
3. Editable input during generation: Enter steers at the next request boundary
   after tools finish; Alt+Enter queues a follow-up after the reply. Queues are
   visible/editable and restored on stop. Shared manual/automatic compaction at
   80% of a known context budget preserves complete tool exchanges and full
   transcript; failed compaction never destroys history. Usage estimates are
   labeled, unknown limits remain unknown.
4. Provider-qualified IDs ollama:<id>, openrouter:<id>; bare IDs remain Ollama.
   OpenRouter uses the native Pydantic adapter, model metadata discovery,
   provider-specific thinking and usage, masked login/logout, environment keys
   and macOS Keychain. Credentials never enter arguments, transcripts or child
   environments. Cloud selection discloses transmission of active history.
5. Shared read/write/edit/grep/find/ls/bash, guarded exact edits and diffs,
   bounded streaming process output and process-group cancellation. !command
   includes output in context; !!command does not. User/project resources:
   settings, hierarchical AGENTS.md, skills/templates/themes/keybindings/reload.
   CLI > trusted project > user > defaults; session choices restore unless
   explicitly overridden; project settings cannot grant authority.
6. MCP stdio/HTTP/OAuth, lifecycle/resources/search/exposure and explicit
   approvals for unknown effects. Native Python extension SDK for tools,
   commands, lifecycle hooks/settings and declarative UI. Explicit local/PyPI/Git
   package install/list/remove/update. Bundle pinned CPython matching the core,
   isolated package environments/workers, host user privileges, atomic updates
   preserving previous environments on failure; no implicit project installs.
7. A separate compose_tools runs Python in the networkless/mountless VM, using
   bounded VM-private JSON request/result files via the control API. First prove
   concurrent guest execution/control operations offline. Shared schemas,
   permissions, approvals and aggregate budgets apply to all indirect calls;
   parallel reads, serialized mutations, call IDs deduplicated. Exclude
   run_code/compose_tools to prevent recursion. Five minutes active execution,
   existing approval timeouts, thirty minutes overall; normal snippet timeout
   unchanged. Stop destroys VM and pending work, never replays side effects.
8. Text/JSON/RPC/Python SDK share controller. Extend private desktop protocol
   additively with capabilities and corresponding workspace/provider/branch/
   queue/compaction/diff/approval/MCP/extension controls. Existing unsupported
   approval interactions fail closed during staged delivery. Combined installer
   includes extension interpreter/workers, signing/notices/sources and 20 GiB
   disk reserve. Update repository rules and user documentation.

## Boundaries

Equivalent coding workflows, not binary Pi extension compatibility. Exclude
other providers, TypeScript runtimes, online sharing, subagents, specialist
classifier/media runtimes, and Windows-specific tools. Local exports only.
Persist completed tool effects, not unfinished answers as subsequent context.
Keep evaluations local/manual/metrics-only. Never alter real user chat data
while developing. Validate with temporary homes and workspaces.

## Validation

Offline terminal input/PTY tests, model/MCP/OAuth fakes, SQLite migration and
concurrency/crash tests, CLI/desktop request parity, guard and cancellation tests,
composition broker feasibility and denial/deduplication/deadline tests. CLI
startup, Black/isort, Python/Swift tests, disposable packaged smoke checks and
one short local prompt when model behavior changes. No automatic live cloud or
benchmark runs. Full parity requires final desktop and packaged checks.
