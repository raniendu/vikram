# 0005. Serve file tools from the harness `FileSystem`, behind Vikram's guard

- **Status:** Accepted
- **Date:** 2026-09-26
- **PR:** raniendu/vikram#40 (WS2)

## Context

Vikram had its own file tools (`read_file`, `write_file`, `edit_file`, `glob`,
`grep`). The harness `FileSystem` capability does the same work with
better editing and search (ripgrep), but its own rules are looser: for
example, it allowed reading `.env`, and a refused path raised `ModelRetry`, which
could end a run if the model kept asking.

## Decision

Use the harness `FileSystem` for the work, wrapped by `vikram/file_tools.py` so
the tool names and Vikram's guarantees stay exactly the same:

- paths stay inside cwd (no `..`, absolute paths or escaping symlinks);
- secrets and excluded folders (`.env*` except `.env.example`, keys, `.ssh/`,
  `secrets/`, `.git/`, `.venv/`, `node_modules/`, ...) can't be read, written
  or listed;
- refusals return `Refusing: ...` and log `tool_call_refused` instead of raising;
- writes go through the same approval prompt, and `PreToolUse`/`PostToolUse`
  hooks still wrap every file tool.

The tools are contributed by a capability subclass (`VikramFileSystem`), not a
bare toolset. The harness only emits its events for tools that come from a
capability, and renaming the toolset breaks those events.

## Consequences

- Less file-handling code of our own; ripgrep-backed search.
- Tool names are unchanged, so specs, prompts and evals needed no edits.
- `rg` must be on `PATH`; the venv's bin folder is appended for `uv tool`
  installs.
- A harness upgrade can change file-tool behaviour; `tests/test_file_tools.py`
  pins the guarantees.

## Revisit when

The harness offers configurable deny rules equivalent to Vikram's; the guard
could then shrink.
