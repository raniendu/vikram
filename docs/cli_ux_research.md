# Coding CLI UX Research

This review compares Vikram's terminal experience with recurring patterns in
current coding CLIs. It focuses on interaction design rather than feature
parity.

## Sources

- [Codex CLI command reference](https://learn.chatgpt.com/docs/developer-commands?surface=cli)
  and [non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-usage)
- [Gemini CLI commands](https://geminicli.com/docs/cli/commands/) and
  [checkpointing](https://geminicli.com/docs/cli/checkpointing/)
- [Aider usage](https://aider.chat/docs/usage.html) and
  [in-chat commands](https://aider.chat/docs/usage/commands.html)

Reviewed July 2026. These products change frequently, so the sources above are
more durable than a copied feature matrix.

## Repeated UX Patterns

### Make capabilities visible at the point of use

Successful CLIs distinguish shell commands from interactive commands, show
short examples, and keep common session actions behind discoverable slash
commands. Aider and Gemini also support contextual command help rather than
requiring users to leave the terminal.

### Pair errors with a recovery path

Claude Code suggests close command names and both Claude Code and Codex expose
read-only diagnostic commands. This turns setup failures into a guided next
step instead of an invitation to inspect a traceback or configuration source.

### Keep trust information close to actions

Coding CLIs show commands or diffs before consequential actions and make the
active model, permissions, workspace, and context visible. Internal adapter
warnings should not appear in the primary task flow because users cannot act on
them and may mistake them for a broken session.

### Optimize the repeat loop

Across Aider, Gemini, and Codex, users can inspect changes, copy the latest
answer, start a fresh task, and resume previous work without reconstructing
context. These actions are more valuable than adding a large number of obscure
flags.

### Treat automation as a separate surface

Codex, Claude Code, and Gemini separate interactive use from headless runs and
provide machine-readable output. Vikram's `exec` command follows this shape
while retaining its older `--once --prompt` form for compatibility.

## Vikram Findings and Changes

1. **Command discovery:** root help previously ended with an unstructured list
   of command names. It now describes each command and shows common examples.
2. **Error recovery:** misspelled commands now suggest the closest command, and
   an empty `exec` prompt explains both argument and stdin usage.
3. **Setup confidence:** `vikram doctor` performs read-only checks for Python,
   configuration validity, spec loading, effective model selection, credential
   presence, command policy, and Git workspace state. JSON output is available
   for support and automation.
4. **Daily review loop:** interactive `/diff` shows staged, unstaged, and
   untracked state; `/copy` copies the last assistant response.
5. **Startup clarity:** the checked-in coder spec no longer requests an adapter
   setting that Vikram intentionally drops, removing a non-actionable warning.

## Deferred Opportunities

- Durable, project-scoped sessions with resume, rename, fork, and ephemeral
  modes.
- Streaming JSONL events with strict stdout/stderr separation for automation.
- A structured permissions view and safer presets beyond the existing command
  policy and approval prompt.
- Checkpoints that pair conversation state with a reversible workspace snapshot.
- Shell completions and richer slash-command completion with descriptions.

Session persistence and checkpoints should be designed together. Adding only a
transcript picker would create a false sense that workspace state can also be
restored.
