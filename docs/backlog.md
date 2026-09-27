# Backlog

Work agreed but not started. Each item says what it is, why, how big it is,
and when it's ready to pick up. Move an item into a PR when you start it, and
delete it here in the same PR.

## Ready when you are

### Edit `[capabilities]` in the desktop app

- **What:** fields in the GUI spec editor for the `[capabilities]` table:
  output limit, compaction, spend limits, prompt-injection mode, secret
  redaction, memory.
- **Why:** today the editor keeps the table when it saves a spec but can't
  show or change it; you have to edit `agent.toml` by hand.
- **Size:** medium. API model, editor UI, round-trip and validation tests.
- **Refs:** `vikram/capabilities.py`, `vikram/spec_io.py`, `docs/capabilities.md`.

## Waiting on evidence

### Block prompt injections instead of only reporting them

- **What:** switch `[capabilities.prompt_injection] mode` from `report` to
  `block`, at least for `vikram` (reachable from Telegram and HTTP). High-risk
  tool results (web pages, files, MCP output) are then withheld from the model.
- **Why:** report mode only logs `prompt_injection_detected`; a real injection
  still reaches the model.
- **Ready when:** a week or two of logs shows the detector fires on real
  attacks, not ordinary pages. Blocking by mistake silently drops content the
  agent needed.
- **Size:** small. One line per spec, tests, and an ADR that supersedes the
  report-mode part of [ADR 0009](adr/0009-injection-defence-and-memory-opt-in.md).

## Housekeeping (manual, not code)

- **Delete merged `claude/*` branches** on GitHub. All of them are fully in
  `main`; a Claude session can't delete branches.
- **Reset eval history and record a fresh baseline**: see "To wipe history and
  start over" in `AGENTS.md` (Evals).

## Revisit if things change

Decisions recorded in ADRs with a condition that would reopen them. Nothing to
do until the condition holds.

| Decision | Reopen when | ADR |
|---|---|---|
| Keep `load_skill` over harness `Skills` | Pydantic AI lets the loader tool be renamed, or the harness lists a skill's bundled files | [0006](adr/0006-keep-load-skill.md) |
| Keep the argv-only command executor | The harness offers argv execution, or commands run inside an OS sandbox | [0007](adr/0007-keep-argv-command-executor.md) |
| Keep `delegate_to_agent` over `SubAgents` | `SubAgents` builds children lazily per surface and hands approvals back | [0008](adr/0008-keep-delegate-to-agent.md) |
| Durable runs on by default | A resumed run misbehaves (duplicate or missing reply, replay error) | [0012](adr/0012-durable-agent-runs-on-by-default.md) |
| Eval triggers (model, framework, prompt) | A regression slips through in a kind that isn't a trigger | [0011](adr/0011-eval-triggers-and-pytest-gate.md) |
