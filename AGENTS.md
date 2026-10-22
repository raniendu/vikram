# Repository Guidelines

## Scope

Vikram is one local assistant built with Pydantic AI and Ollama. Keep the
implementation small. The owner approved the local Tauri chat app in
`docs/superpowers/specs/2026-10-14-local-chat-design.md`. Its persistence, private
Python sidecar and focused offline tests are authorized. The owner approved
document attachments and Parallel web search in
`docs/superpowers/specs/2026-10-14-documents-and-web-search-design.md`. CLI and
desktop must share model/tool instructions, capabilities, execution, limits,
failure handling, and context policy; keep that behavior outside UI adapters.
CLI and desktop chats always use automatic web tools when the model supports
them, including desktop chats with a legacy Web Off preference. Neither
interface exposes a web on/off setting.
The owner requested visible model thinking and a modern terminal chat. Thinking
is on by default in the CLI, desktop, and evaluations, and off only when
explicitly chosen; "on" is a model's `true` control, else its own named level.
Desktop
replies save bounded thinking separately from the answer and show it collapsed
by default. Terminal traces are in memory only and shown on demand. Never add
these display traces to follow-up context, logs, or evaluation artifacts.
The owner requested the combined CLI/Mac installer and `vikram update`; its
design is in `docs/superpowers/specs/2026-10-14-install-and-update-design.md` and
distribution instructions are in `docs/releases.md`. Build local artifacts and
validate with disposable install directories; publishing requires owner direction.
The owner requested clarifying questions, plan mode, and a Python sandbox built
on Pydantic libraries; the design is in
`docs/superpowers/specs/2026-10-20-questions-plan-mode-sandbox-design.md`.
Keep them in the shared toolkit with CLI/desktop parity. The sandbox is a local
microVM per reply (Harness SmolSandbox on smolvm) booted from a bundled,
digest-pinned image. Never run model code outside a VM, and keep the VM free of
mounts, host environment, host tools, and network without owner direction.
The owner requested an agent-controlled browser and chose its engine, safety
policy, and CLI behavior; the design is in
`docs/superpowers/specs/2026-10-21-agent-browser-design.md`. The engine is a
WebKit child webview: a panel in the app the person can watch, take over, stop,
or close, and the same app binary as a hidden `--browser-helper` for the CLI.
Keep its non-persistent profile, its page script in a separate script world,
approval before consequential actions (form submissions including Enter,
sign-in, purchases, posting, deleting, agreeing), refusal of password, payment,
and one-time-code fields, blocked downloads, uploads, pop-ups, and private or
local network addresses, and the Tauri capability that excludes the panel.
Page content is untrusted data. Do not add another browser engine, bundle
Chromium, or give the sandbox VM network access without owner direction.
Evaluations must explicitly use web Off; tool-capable models keep the local
toolkit with a scripted user, and the browser case uses an offline scripted
site. Do not revive the discarded software factory or add other agents,
unrelated tools, hosted inference, or public server surfaces.

## Structure

- `vikram/agent.py`: the single agent definition, inline instructions, default
  model, and `build_agent(model: str = DEFAULT_MODEL) -> Agent`.
- `vikram/cli.py`: interactive and one-shot command-line conversations.
- `vikram/terminal.py`: inline streaming Markdown, input editing, and optional thinking.
- `vikram/conversation.py`, `context.py`, `models.py`, `web.py`: shared model/tool behavior.
- `vikram/toolkit.py`, `interaction.py`: sandbox, questions, plan mode, browser
  tools, and the answer/review/approval contract each interface implements.
- `vikram/sandbox.py`: the per-reply Python microVM, its pinned image, and cleanup.
- `vikram/browser.py`: the browser engine contract, page views, argument repair,
  and the CLI's hidden helper process.
- `apps/desktop/src-tauri/src/browser.rs`, `browser.js`: the WebKit browser
  (panel or helper), its navigation policy and content blocker, and the page
  script that numbers elements and classifies consequential actions.
- `vikram/documents.py`, `document_worker.py`: bounded local document parsing.
- `vikram/__main__.py`: entry point for `python -m vikram`.
- `vikram/desktop/`: versioned private stdio runtime, chat store and attachments.
- `apps/desktop/`: React/Vite UI and Tauri 2 Mac shell; bundled native Python.
- `scripts/build_desktop.py`: reproducible sidecar/app packaging, disk reserve check.
- `scripts/fetch_sandbox_image.py`: fetches and verifies the pinned sandbox image.
- `scripts/build_installer.py`: combined per-user CLI/Mac installer artifact.
- `vikram/installation.py`, `releases.py`: atomic bundle installation and GitHub updates.
- `tests/`: focused offline chat and recovery checks.
- `vikram/ollama.py`: shared thinking metadata validation and request mapping.
- `vikram/evals/`: manually invoked Pydantic Evals, local Ollama rubric grading,
  metrics-only history, a self-contained HTML report, and the offline site the
  browser case uses (`site.py`).

For the original CLI, the model endpoint is fixed to `http://localhost:11434/v1`. Model selection is
`--model`, then `OLLAMA_MODEL`, then the default in `agent.py`. The application
does not load configuration files or `.env` files. Interactive history stays
in memory for the current process.

Evaluations reuse this agent with fresh history per attempt. Both answers and
rubric grading use local Ollama; no API key is needed. Generate all cases and
thinking levels for each model in sequence, then grade all queued answers with
one fixed judge (default Gemma 4 26B, thinking off). Never interleave judge calls
with agent generation. Keep pending answers only in memory and save only metrics.
On cancellation, preserve scored counts and mark unjudged answers unscored.
Evaluation artifacts live under the Git-ignored `.vikram/evals/v1/`. Preserve
older local state, including historical cloud-judge records.

## Commands

- `uv sync --locked`: install dependencies.
- `uv run vikram`: start an interactive conversation.
- `uv run vikram "Hello"`: run one prompt.
- `uv run vikram --model MODEL_NAME "Hello"`: use another local Ollama model.
- `uv run vikram --plan "Task"`: start in plan mode (`/plan` toggles it in chat).
- `uv run vikram --help`: check CLI startup without calling Ollama.
- `uv run vikram-eval run`: benchmark the twelve cases (eight starter, four
  tool cases), five attempts each with thinking on. Add `--thinking all` to
  sweep every level Ollama advertises; repeat `--model` for several models.
- `uv run vikram-eval run --label "Change description" --experiment "Comparison"`:
  annotate a run and group deliberate comparisons.
- `uv run vikram-eval run --judge-model MODEL --judge-thinking off`: use a fixed
  local judge independently of the models and thinking levels being benchmarked.
- `uv run vikram-eval run --case arithmetic --samples 1 --k 1 --thinking default`:
  local smoke eval without a thinking sweep.
- `uv run vikram-eval report`: rebuild the offline HTML report from metrics.
- `uv run vikram-eval --help`: check evaluation CLI startup without model calls.
- `uv run black --check vikram`: check formatting.
- `uv run isort --check-only vikram`: check import ordering.
- `uv run pre-commit install`: install optional formatting hooks.

Desktop commands:

- `uv run python scripts/build_desktop.py`: build native sidecar and `.app`.
- `uv run python scripts/build_installer.py`: build combined installer and release ZIP.
- `uv run python scripts/check_installer.py dist/installers/Vikram-0.1.0-macos-arm64.zip`: isolated packaged-install smoke check.
- `uv run vikram update --check`: check the latest stable GitHub release without installation.
- `npm --prefix apps/desktop run desktop`: start Tauri development mode after building the sidecar.
- `apps/desktop/src-tauri/target/release/bundle/macos/Vikram.app/Contents/MacOS/vikram-sidecar --check-browser`:
  start the packaged browser helper on a blank page without the network.
- `uv run python -m unittest discover -s tests -v`: offline Python checks.
- `npm --prefix apps/desktop test`: frontend recovery checks.
- `npm --prefix apps/desktop run build`: TypeScript and production assets.
- `cargo test --offline --manifest-path apps/desktop/src-tauri/Cargo.toml`: native lifecycle check.

## Working Style

Inspect relevant files first and prefer small, maintainable changes. Preserve
unrelated local work. Do not commit, push, open pull requests, or delete user
state unless explicitly asked.

Before merging any pull request, present its link, final changes, and check
results, then obtain explicit owner approval for that specific merge. Do not
enable auto-merge or bypass approval by pushing to a protected/default branch.
Material changes after approval require renewed approval.

Use Python 3.13 or later, type hints at public boundaries, Black's 88-character
line length, and isort's Black profile. Keep documentation consistent with the
code.

Use focused offline tests for desktop storage, IPC, metadata, cancellation and
UI recovery. Do not run live model evaluations automatically. Desktop chat data
lives under `~/Library/Application Support/dev.raniendu.vikram/`; never mix it
with evaluation metrics. Keep at least 20 GiB free on the internal disk.
Validate changes with the CLI startup check and formatters. When model behavior
changes, check it with a short local Ollama prompt. Report what was validated
and any limitations.

Never commit credentials, private keys, populated environment files, or local
runtime state. Do not add logging of prompts, conversation content, or secrets.
Evaluation history must contain only typed metrics and provenance, never full
Pydantic Evals reports, prompts, outputs, judge reasons, or exception bodies.
Keep cases and graders explicit. Do not run live evaluations automatically in CI
or Git hooks. Treat model/evaluator errors as unscored, not incorrect answers.

Keep eval-series identity separate from agent configuration identity. The same
cases and grading policy remain comparable across model, prompt, runtime/tool,
dependency, and harness changes. Record fingerprints and versions without source
content; do not infer complete provenance for legacy runs. Experiment labels
group runs but never establish that only one variable changed. Preserve history
when generating the report, and keep incomplete scores distinct from zero.

Show pass@1 and pass@3 together in the benchmark report. Keep suites visible in
separate sections and thinking levels in separate rows; avoid display filters.
Label charts, findings, and tables with actual model names, not C1/C2 aliases.
In the quality/time chart, connect thinking levels within each model and agent
revision. Show every supported level, leaving gaps for missing results.
Discover supported thinking controls from Ollama metadata. Never infer missing
levels, pool different levels, or store thinking traces in evaluations. Explicit defaults and
untested supported levels must remain distinguishable from scored results.
