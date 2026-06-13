# Using hooks

Hooks let an agent run your own code at four lifecycle events, to observe,
augment, or block what the agent is about to do. They are declared per agent in
`spec/<agent>/agent.toml` under `[[hooks]]` and assembled into the agent by
`vikram.agent.build_agent`, so — like skills and MCP servers — they apply on
**every surface** the agent runs on (CLI, ACP, HTTP `/chat`, threaded queues,
Telegram).

This is the same idea as hooks in other CLI agents: a hook is a small handler
that receives a JSON description of what's happening and can answer back with a
decision.

## Events

| Event | Fires | Can block? | A blocking decision… |
| --- | --- | --- | --- |
| `PreToolUse` | Before a tool runs | Yes | Stops the call; the reason is returned to the model so it can adjust. |
| `PostToolUse` | After a tool returns | Yes | Asks the model to reconsider; otherwise extra context is appended to the result. |
| `UserPromptSubmit` | When a prompt enters a run | Yes | Aborts the run (`HookBlockedError`); otherwise extra context is prepended to the prompt. |
| `Stop` | When a run finishes | No | Advisory only — use for notifications or logging. |

`PreToolUse` and `PostToolUse` wrap the combined toolset, so they intercept both
the agent's built-in tools **and** any MCP server tools.

## Transports

A hook handler is either an external program or an in-process Python callable.

| `transport` | Handler | Use for |
| --- | --- | --- |
| `command` (default) | A program, given the payload as JSON on stdin | Language-agnostic policies, shelling out to existing tooling |
| `python` | A `module:function` callable, imported when the agent is built | Fast in-process checks, no subprocess overhead |

### `command` handlers

The program receives the event payload as a single JSON object on **stdin** and
controls the outcome through its **exit code** and **stdout**:

- **Exit `0`** — allow. If stdout is a JSON object it refines the decision (see
  [Decisions](#decisions)); if stdout is plain text it is treated as
  `additional_context`; empty stdout is a no-op.
- **Exit `2`** — block the action. Whatever the program wrote to **stderr** is
  used as the reason shown to the model.
- **Any other non-zero exit** — a non-blocking error. It is logged and ignored
  so a broken hook can't brick the agent.

```toml
[[hooks]]
event = "PreToolUse"
matcher = "run_command"          # only for run_command calls
transport = "command"
command = "./hooks/guard.sh"
args = ["--strict"]
env = { TOKEN = "${GUARD_TOKEN}" }   # ${ENV_VAR} expansion, like MCP specs
timeout = 30                          # seconds; on timeout the hook is ignored
```

Example `./hooks/guard.sh` that blocks any command containing `rm -rf`:

```bash
#!/usr/bin/env bash
payload="$(cat)"
if printf '%s' "$payload" | grep -q 'rm -rf'; then
  echo "Refusing destructive command" >&2
  exit 2
fi
```

### `python` handlers

The callable is referenced as `module:function`, imported at agent-build time
(so a bad reference fails loudly, not mid-run), and called with the payload
`dict`. It may be sync or async and returns:

- `None` — allow (no-op).
- a `dict` — a [decision](#decisions).
- a `str` — shorthand for `{"additional_context": "<str>"}`.

Any exception it raises is logged and treated as non-blocking.

```toml
[[hooks]]
event = "PostToolUse"
transport = "python"
entrypoint = "myhooks.audit:on_tool"
```

```python
# myhooks/audit.py
def on_tool(payload: dict) -> dict | None:
    if payload["tool_name"] == "write_file":
        return {"additional_context": "File written — remember to run tests."}
    return None
```

The module must be importable by the Vikram process (on `PYTHONPATH`, or part of
an installed package).

## The payload

Every handler receives a JSON object with at least `event`, `agent`, and `cwd`.
Event-specific fields:

| Field | Present for | Meaning |
| --- | --- | --- |
| `tool_name` | `PreToolUse`, `PostToolUse` | The tool being called. |
| `tool_input` | `PreToolUse`, `PostToolUse` | The tool's arguments (object). |
| `tool_output` | `PostToolUse` | The tool's result, as a string. |
| `prompt` | `UserPromptSubmit` | The user's prompt text. |
| `output` | `Stop` | The agent's final output, if available. |

## Decisions

A handler's response (stdout JSON for `command`, return value for `python`) uses
these optional keys:

```json
{
  "decision": "allow" | "deny" | "block",
  "reason": "shown to the model when blocked",
  "additional_context": "injected or appended text"
}
```

- `decision` of `deny` or `block` blocks the action; everything else allows it.
  (A `command` hook can also block simply by exiting `2`.)
- `additional_context` is **prepended** to the prompt for `UserPromptSubmit` and
  **appended** to the result for `PostToolUse`. It is ignored for `PreToolUse`
  and `Stop`.

When several hooks match one event, they run in declaration order; if **any**
blocks, the action is blocked and all reasons are combined.

## The `matcher`

For the tool events, `matcher` is a glob tested against the tool name (default
`*` matches every tool). Use it to scope a hook to specific tools:

```toml
matcher = "run_command"     # exactly run_command
matcher = "*_file"          # read_file, write_file, edit_file
matcher = "*"               # all tools (default)
```

`matcher` is ignored for `UserPromptSubmit` and `Stop`, which aren't tied to a
tool.

## Approval interaction

For tools that **statically** require human approval (`write_file`,
`edit_file`), the call only reaches the hook layer after the human has approved,
so their `PreToolUse` hooks run *post-approval* — they can still block, but the
approval prompt comes first. `run_command` decides approval dynamically inside
its own body, so its `PreToolUse` hook runs **before** any approval prompt and
is the right place to enforce command policy beyond
`spec/shared/command_policy.toml`.

## Secrets and `${ENV_VAR}` expansion

As with MCP specs, never write secrets inline. The `command`, `args`, `cwd`, and
the values of `env` may contain `${VAR}` references, expanded from the process
environment when the agent is built. A reference to an undefined variable is a
hard error (`HookConfigError`) naming the field, so misconfiguration fails at
startup rather than silently.

## Inspect what an agent has

You don't need a configured model to see what a spec will attach:

```bash
uv run python -c "
from vikram.settings import VikramSettings
from vikram.spec import load_spec
from vikram.hooks import build_hooks

spec = load_spec('coder', VikramSettings().spec_root)
hooks = build_hooks(spec.hooks)
print('tool hooks:', [h.event for h in (*hooks.pre, *hooks.post)])
print('run hooks: ', [h.event for h in (*hooks.user_prompt_submit, *hooks.stop)])
"
```

This also expands `${ENV_VAR}` references and imports `python` entrypoints, so
it surfaces missing variables or bad references early.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `HookConfigError: ... uses command transport but has no 'command'` | Add `command` to the `[[hooks]]` entry. |
| `HookConfigError: ... uses python transport but has no 'entrypoint'` | Add `entrypoint = "module:function"`. |
| `HookConfigError: ... entrypoint '...' must be in 'module:function' form` | Use a colon: `pkg.mod:func`. |
| `HookConfigError: ... could not import module` | The module isn't importable by the Vikram process; check `PYTHONPATH`/install. |
| `HookConfigError: ... references undefined environment variable ${X}` | Set `X` in `.env` or the environment. |
| A `command` hook never blocks | Block with **exit code 2** (or `{"decision":"deny"}` on stdout). Other non-zero codes are ignored by design. |
| A hook seems skipped for some tools | Check its `matcher` glob against the tool name. |
