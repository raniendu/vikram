# Vikram Studio (desktop app)

A macOS app for building agents, running them against a workspace, and
comparing models. It is a thin Tauri shell over the same `build_agent` factory
the CLI uses — the GUI is a fifth surface, not a second runtime.

## Install and run

```bash
# 1. Install the runtime (this also provides vikram-api, which the app runs)
uv tool install --force --reinstall-package vikram --python 3.13 --from . vikram

# 2. Build the app once
cd gui && npm install && npm run tauri build && cd ..

# 3. Install it
cp -R "gui/src-tauri/target/release/bundle/macos/Vikram Studio.app" ~/Applications/

# 4. Open it
vikram gui
```

`--reinstall-package` is deliberate: without it uv reuses a cached wheel for
`vikram==0.1.0` and the upgrade silently no-ops.

### Always launch with `vikram gui`

Not a style preference. macOS apps started from Finder, Spotlight or the Dock
inherit **no login shell PATH**, so `~/.local/bin/vikram-api` — where
`uv tool install` puts it — is invisible to them. `vikram gui` runs in a shell
that can see it, resolves an absolute path, and passes it to the app in
`VIKRAM_API_BIN`. The app also probes `~/.local/bin`, `~/.cargo/bin`,
`/opt/homebrew/bin` and `/usr/local/bin`, and shows a named error rather than
an endless splash screen if it still finds nothing.

### Running from the checkout

```bash
vikram gui --dev     # vite + tauri dev, hot reload
```

Requires Rust:

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

## What is in it

| Screen | What it does |
|---|---|
| **Agents** | Every agent from both roots. Built-ins are read-only; editing one copies it to your own root first. |
| **Editor** | Identity, system prompt, tool picker with approval badges, model and `model_settings`, MCP servers with a Test button, raw TOML. Validate dry-runs the spec and shows the assembled prompt. |
| **Chat** | Pick a workspace folder, run the agent, answer approvals in a native dialog. |
| **Playground** | One agent, one prompt, 2–4 models side by side with time-to-first-token, total time and token counts. |
| **Settings** | Providers, models, base URLs, API keys. Keys are written to config and never sent back to the window. |
| **Doctor** | The same checks as `vikram doctor`. |

## Where your agents live

| Path | Role |
|---|---|
| `~/.config/vikram/agents/` | Your agents. Writable, created by the app. |
| `<spec_root>/` | Shipped agents. Read-only; also the source of `shared/`. |

A user agent shadows a built-in of the same id, so editing `coder` leaves the
shipped spec untouched and deleting your copy restores it. Agents created here
work everywhere — `vikram --agent my-agent`, `vikram doctor --agent my-agent`,
and delegation from the orchestrator.

Comments survive edits: the writer round-trips the file with tomlkit rather
than re-emitting it.

## Security

`vikram-api` is a deployment target that `Dockerfile` serves on `0.0.0.0`, and
these endpoints reach the agent store and shell-capable sessions. Four
independent controls keep them off a deployed instance:

1. **The router is not mounted by default.** `/v1/*` returns 404 unless
   `--gui` / `VIKRAM_GUI_ENABLED=1`.
2. **Every route needs a bearer token**, generated per launch by the app and
   passed in the child's environment — never argv, which `ps` exposes.
3. **`--gui` refuses a non-loopback host.**
4. **A watchdog kills the API if the window dies**, so a crashed shell cannot
   leave a shell-capable server listening.

No response carries credential material: providers report `has_credential` and
the env var *name*, `/v1/config` reduces `api_key` to a boolean, and MCP
responses redact `url`, `command`, `args` and the values of `env`/`headers`,
which can hold expanded `${VAR}` secrets.

## How a session runs

Each session gets its **own process**. Two pieces of runtime state are
process-global: the command policy written by `set_command_policy`, and the
workspace root, which is `Path.cwd()`. One process can therefore host exactly
one (agent, workspace) pair. Killing the process group also reaps `run_command`
children and MCP stdio servers.

The worker speaks newline-delimited JSON over stdio (the same shape `acp.py`
uses) and holds the agent in `async with` for the session, so MCP servers start
once rather than per turn.

Approvals arrive as structured events — tool name and typed arguments — and the
worker waits on the answer for up to 300 seconds before denying, so a closed
window cannot leave it holding a `sequential` tool lock.

## Why the playground can share one process

It varies only the *model*, holding the agent and workspace constant, so every
column has an identical command policy and cwd — the two hazards that force
separate processes elsewhere. Fanning out over *agents* would not be safe and
the app does not do it.

Approval-gated tools are disabled for comparisons. Four columns approving the
same `write_file` and then each executing it against one workspace is both poor
UX and a correctness hazard, so the runtime's existing auto-deny branch applies
and each column reports the refusal.

Expect columns to run slower than they would alone — they contend for the same
GPU. Time-to-first-token is measured inside the worker, so HTTP and SSE latency
do not pollute the comparison.

## Troubleshooting

**"Can't find vikram-api"** — launch with `vikram gui`, or check
`which vikram-api`. If it is missing, re-run the `uv tool install` above.

**The app opens but shows an error immediately** — run `vikram doctor`. The
same checks are on the Doctor screen once the API starts.

**A model 404s** — the spec pins a model you no longer have. Check
`ollama list`, then fix it in the editor's Model section or with `/model` in
the CLI.

**An agent is listed with "Spec will not load"** — the TOML is malformed. The
app lists it anyway so you can repair it; a broken spec no longer breaks the
other agents.
