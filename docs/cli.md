# Terminal workbench

Start `vikram` (or `uv run vikram` from a checkout). `/model`, Ctrl+L or F2 discovers
installed Ollama models and shows availability, tool/image support and thinking
controls. Refresh keeps your draft. `/models` chooses the subset used by Ctrl+P;
Shift+Tab opens thinking choices. `/settings` controls theme, display and editor
preferences. `/reload` reloads trusted resources and integrations.

If the selected model is missing, the model picker opens automatically. Choose an
installed model once and Vikram remembers it on this Mac for new CLI and desktop
conversations. `/settings` → **Default model** or `/default-model` chooses a
different default without changing the open conversation. The Mac app exposes
the same control in **Settings → Default Model** and **Workbench → Models**.
An explicit `--model` or `OLLAMA_MODEL` still takes precedence for that invocation;
resumed conversations retain their own model, and trusted project settings can
override the app default. Explicitly saving an OpenRouter default also applies
to future conversations, with the cloud disclosure shown before selection.

The fullscreen editor stays usable during generation. Enter sends steering at
the next model request boundary; Alt+Enter queues a follow-up. `/queue` edits or
removes pending messages. Stopping retains unsent entries. Ctrl+G opens the
configured external editor; Ctrl+X copies the latest answer; Ctrl+F searches the
transcript. `/hotkeys` lists all bindings. `--tui-mode regular` keeps completed
messages in terminal scrollback; `--tui-mode plain` selects the plain loop.

## Sessions and files

CLI and desktop share `~/Library/Application Support/dev.raniendu.vikram/`.
`--session-dir DIRECTORY` (or `VIKRAM_CHAT_DATA_DIR`) selects a separate library.
`--no-session` disables chat persistence. Do not run two controllers in the same
session; choose another session or clone it. Other sessions remain available.

Use `/resume` to search, rename or delete conversations; `/name` renames the
current one. `/tree` selects a preserved branch; `/fork` copies a path and
`/clone` copies the conversation. Branch changes never roll back workspace files.
`/compact` summarizes older turns while retaining the full transcript. Automatic
compaction runs at 80% of a known context limit; unknown limits stay unknown.
Token estimates are labeled separately from provider-reported usage and cost.

Use `/attach PATH`, `@path` completion or `vikram @notes.pdf "Summarize this"`.
Files are bounded to four per message, 10 MiB each. `/attachments` removes pending
files. Image-capable models accept PNG, JPEG and WebP; clipboard image import and
quoted dropped paths use the same validation. Text-only models do not silently
consume images. Ctrl+G edits text with the configured editor.

`/export` writes local Markdown, HTML or versioned JSONL; `/import` creates a new
conversation from an export. JSONL retains branches and attachments. Imports do
not import workspace trust or credentials. Exports never include thinking traces.

## Workspace tools and instructions

Choose `/workspace`, then `/trust` to enable host coding. Trust is stored against
the canonical directory in user settings. Desktop has the same controls in
Workbench. The model receives `read`, `write`, `edit`, `grep`, `find`, `ls`, and
`bash`. Exact edits reject stale or ambiguous content and show diffs. Host tools
run with your user privileges. Unknown shell effects, destructive operations,
outside-workspace access, credential access, installation and remote mutations
require approval. Plan mode permits inspection and waits for an approved plan
before effects. Stop terminates command process groups.

`!command` runs a user-requested command and includes its bounded output in
context; `!!command` displays it without adding it to context. These commands
never imply that host execution is sandboxed. Interrupted effects are retained
as completed or uncertain actions, with instructions to inspect before retrying.

User settings are `settings.json` beside the chat library. Trusted project values
in `.vikram/settings.json` override user values; CLI overrides take precedence.
Project files cannot grant trust, configure integrations or choose cloud models.
Supported values include `model`, `models`, `thinking`, `theme`, `interface`,
`show_thinking`, `show_tools`, `auto_compact` and `keybindings`. User-only values
include `editor`, `context_limit`, `mcp_servers` and `extensions`. Credentials are
never settings. Do not put secrets in JSON files.

Root and nested `AGENTS.md` instructions apply in their directory scope. User and
trusted project resource directories supply `skills/*/SKILL.md`, `templates/*.md`,
`themes/*.json` and `keybindings.json`; project `.agents/skills` is supported.
`/skill:name` loads a skill; named prompt templates expand arguments. `/reload`
refreshes resources without restarting the conversation.

## OpenRouter

Ollama is the default and there is no automatic cloud fallback. `/login` accepts
an OpenRouter key through masked input and stores it in macOS Keychain;
`OPENROUTER_API_KEY` is an alternative. `/logout` removes the stored key; an
environment-supplied key remains until unset. Keys are never command arguments
or child-process environment entries.

The model picker explicitly connects OpenRouter. Selecting a cloud model sends
the active conversation and attachments to OpenRouter and its model provider.
CLI selection uses `--model openrouter:provider/model`; local aliases use
`--model ollama:MODEL` or the original bare ID. Missing credentials or unavailable
models preserve the draft and never select another provider automatically.

## MCP and Python extensions

`/mcp` adds, enables, disables, reconnects or removes servers. Stdio configuration
uses `{"command":"/path/to/server","args":[]}`; HTTP uses
`{"transport":"http","url":"https://server.example/mcp","oauth":true}`.
OAuth Sign In opens your browser and receives its callback on the configured
loopback redirect (`http://127.0.0.1:8989/callback` by default). Tokens live in
Keychain. Plaintext authentication headers are rejected.

MCP tools are discovered with bounded pagination. `exposed` lists eagerly offered
tools; `mcp_search_tools` makes matching tools available on subsequent model
requests. Resource and URI-template listing and resource reads use the same
manager. Unknown effects require approval; server annotations alone grant no
permission. User configuration can explicitly list `read_only` tool names and
`read_only_resources` URIs.

`/packages` installs, lists, enables, disables, updates or removes native Python
extensions from local paths, PyPI requirements or Git sources. Installation is
explicit; project files never install packages. Each package runs in a private
worker/environment using bundled pinned CPython, with host user privileges.
Failed updates retain the previous environment. Commands, settings, lifecycle
hooks and inert text/table/progress declarations are available in both adapters.
See [the extension SDK](extensions.md). Lifecycle events are `before_reply` and
`after_reply`; thinking traces are excluded.

When workspace or integration tools are available and this host supports the
sandbox, `compose_tools` lets the model orchestrate them with Python in a fresh,
networkless, mountless VM. `await call_tool(name, **arguments)` dispatches through
the same schemas, policies, approval UI and budgets as direct calls. Reads can
run concurrently, mutations serialize, call IDs prevent replay, and recursive
VM calls are unavailable. The limits are five minutes of active execution,
excluding approval waits, and thirty minutes overall. Stop destroys the VM.

## Automation

```sh
vikram --list-models
vikram --continue
vikram --session SESSION_ID "Continue"
cat request.txt | vikram --no-session --print
vikram --mode json --no-session "Hello"
vikram --mode rpc --session-dir /path/to/library
```

Text, version-1 JSON events, RPC and `vikram.sdk.Session` use the shared model and
tool core. JSON events omit thinking traces. RPC uses newline-delimited
`{"v":1,"id":"request-id","method":"hello","params":{}}` commands and the
additive desktop protocol, including its advertised capabilities and workbench
operations. It is Vikram's protocol, not binary Pi compatibility. Noninteractive
runs decline interactions they cannot show. Legacy scheduled prompts and
evaluations keep the local-only policy and do not load host integrations.
Bot runs have explicit per-bot OpenRouter permission, but still no host tools.

## Bots and attention

`vikram home` shows decisions/failures, material developments, and bot health.
`vikram home --json` exposes the same typed records as the app. Create bots
without a chat:

```sh
vikram bots templates
vikram bots create "Immigration" --template topic-watcher \\
  --mandate "Report official immigration rule and deadline changes, not commentary." \\
  --source https://www.uscis.gov/newsroom --model YOUR_LOCAL_MODEL
vikram bots show Immigration
vikram bots run Immigration
vikram findings --json
vikram findings seen FINDING_ID
vikram findings snooze FINDING_ID week
vikram findings not-material FINDING_ID
```

Creation accepts `--repeat`, `--time`, `--days`, `--every-minutes`, `--date`, and
`--day-of-month`; templates supply a default. `--budget-minutes`, `--max-requests`
(at most 10), and `--max-tool-calls` (at most 20) bound each run. `--expected-days`
flags missing completed runs. `--rubric` and repeatable `--keyword` configure
materiality. `--notify attention|findings|never` controls macOS notifications;
quiet never notifies. The app's Script Editor notification-permission limitation
still applies. No notification contains finding content.

Omit `--model` to inherit the shared default model. That does not permit cloud
use: `--allow-openrouter` explicitly grants it for this bot, with no automatic
fallback. Cloud instructions, sources, and context go to OpenRouter and its
provider; charges may apply. **There is no monetary spending cap yet.** The
request, tool, and time limits are not a dollar cap.

`vikram bots pause|resume|delete|reset NAME` controls the bot. Reset resolves its
open findings and forgets its baseline, not its run evidence. `vikram bots run`
runs in the foreground under the shared runner lock and also drains other due
tasks. Normal installed runs use the existing LaunchAgent even with the app
closed; development libraries never register one.

```sh
vikram bots export Immigration > immigration.json
vikram bots import immigration.json
vikram bots edit Immigration immigration.json
```

Import strips cloud permission and starts paused. Edit changes the existing bot;
adding cloud permission via a file still requires `--allow-openrouter`. Configs
are versioned JSON, not executable code. `vikram findings --all` includes closed
findings; `--bot NAME` filters them. Findings are evidence and proposals, not an
authorization to act. OpenRouter selection never grants browser/host access.

Topic, Page, and Release Watcher templates use search/fetch and the offline
Python sandbox. Dependency Keeper is listed as blocked: updating and testing
repositories requires a separately approved repository VM. No old external
schedules are imported, paused, or recreated by these commands automatically.
See the [implementation inventory](superpowers/specs/2026-11-10-bots-and-home-design.md)
for remaining backlog items.
