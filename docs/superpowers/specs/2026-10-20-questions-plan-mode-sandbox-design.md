# Questions, plan mode, and a Python sandbox

Date: October 20, 2026

Status: requested by the owner on October 20, 2026 ("implement Ask User Question
tool, Plan Mode and a Sandbox for code execution using Pydantic related
libraries"), then implemented with CLI/desktop parity, evaluations, and offline
tests. This extends the [documents and web search design](2026-10-14-documents-and-web-search-design.md).

Revised the same day: the owner asked "Can we isolate it to a VM?", tested a
microVM, then chose to replace Monty with it, failing closed, with the image
bundled in the app. Section 5 records the VM design.

## 1. Outcome

Tool-capable local models get three local tools in both interfaces:

- **Sandbox:** `run_code` executes model-written Python for exact arithmetic,
  counting, dates, and data processing, then reports the result.
- **Questions:** `ask_user_question` asks the person multiple-choice questions
  (with a typed answer allowed) when a request is ambiguous, and waits.
- **Plan mode:** a per-conversation mode in which Vikram must submit a plan with
  `submit_plan` and get approval before it runs code or does the work.

Models without tool calling keep normal chat. In plan mode they are told to
reply with a plan only.

## 2. Pydantic libraries

- [Pydantic AI Harness](https://pydantic.dev/docs/ai/harness/) `0.56.0`, which
  pins the repository's `pydantic-ai-slim==2.56.0` exactly:
  - `SmolSandboxBackend` runs `run_code` in a local
    [smolvm](https://github.com/smol-machines/smolvm) microVM through Pydantic
    AI's `Workspace` API (`smolmachines==1.29.1`).
  - `AskUser` provides `ask_user_question`, its schema, validation, and the
    answerer protocol each interface implements.
- Core Pydantic AI deferred tools: `submit_plan` uses `requires_approval=True`,
  resolved inline by `HandleDeferredToolCalls`, so the plan tool's body (which
  ends plan mode) cannot run without approval.
- Pydantic Evals attributes carry tool completion into a `ToolCheck` evaluator.

## 3. Shared behavior

`vikram/toolkit.py` owns all model-facing behavior, so the CLI and desktop send
identical requests (a parity test covers a question, plan, and sandbox flow).
`vikram/interaction.py` defines the only adapter contract: `ask(request)` and
`review_plan(request)`. Adapters present and collect; they never decide what the
model is told.

Per-reply state (mode, the person, the reply's VM, and visible activity
records) travels as the agent's `deps`, so evaluations can reuse one agent
across attempts. A wrapper toolset decides which tools each request offers:

| Mode | Offered tools |
| --- | --- |
| Normal | web tools (when available), `ask_user_question`, `run_code` (when this Mac can run the VM) |
| Plan, before approval | web tools, `ask_user_question`, `submit_plan`, and `run_code`, which is refused |
| Final request of a reply | none, so the model must answer |

In plan mode, `run_code` stays offered but every call is refused with a finished
result telling the model to submit its plan; no code runs. Hiding it failed in
live testing: Gemma 4 26B (thinking off) kept calling the unoffered tool, and
"unknown tool" retries ended the reply. Refusals spend no retries.

Approval ends plan mode for the rest of the reply. Plan-mode instructions are
recomputed per request and drop away, and the sandbox becomes available. The
desktop chat (and CLI session) also leaves plan mode, as a person would expect
after approving. A request for changes returns the person's feedback to the
model, and "Not now" tells the model to present the plan and stop.

Small local models often send malformed question arguments: string options, a
missing `question`, long headers, or extra keys. A `before_tool_validate` hook
repairs these into the Harness schema, and strict validation still runs. It was
added after live testing showed Gemma 4 26B (thinking off) failing every call
without it.

## 4. Bounds

- At most 10 model requests per reply (was 5); the last offers no tools.
- Web limits are unchanged: three remote calls and 60 seconds of tool time.
- Per reply: 6 sandbox runs, 3 question calls, 3 plan submissions. Past a limit
  the tool returns a failed result the model must adapt to.
- Each snippet: 15 seconds in a VM with 1 CPU, 512 MiB, and 1 GB disks.
  smolvm's default disk is 19.6 GB and the VM writes over 1 GB/s, so without the
  cap one snippet could fill most of the Mac's free space. A VM also does not
  boot if its disks could take the Mac below the 20 GiB reserve. Results reach
  the model and the UI bounded to 8,000 characters; code records to 20,000 and
  plans to 6,000.
- Errors, timeouts, and a VM that cannot start return as finished failures with
  the traceback or reason, so a bad snippet never ends the reply. The model can
  fix the code within the run limit.
- Questions and plan reviews wait at most 15 minutes, then continue as
  declined or not approved. Stop/Ctrl+C ends the wait and the reply.

## 5. Sandbox isolation

`run_code` runs model-written Python only in a local microVM (`vikram/sandbox.py`):

- **Isolation.** smolvm boots its own Linux kernel on Apple's Hypervisor
  framework. Vikram mounts nothing from the Mac, passes no environment, and
  keeps the VM's network off. A live test checks the VM cannot see the Mac's
  home directory, a host environment variable, or the internet.
- **Image.** A digest-pinned `python:3.13-alpine` linux/arm64 image (standard
  library only), fetched at build time by `scripts/fetch_sandbox_image.py` and
  bundled in the app as a local archive, so code never needs the network.
  Before use, every file in the archive is checked against the pinned manifest
  digest.
- **Lifecycle.** Each reply that runs code gets a new VM, booted on first use
  (about 1.5 s the first time, 0.3 s once the image is cached) and deleted when
  the reply ends, including on stop. Each call is a fresh Python process, and a
  final bare expression is printed as in the REPL; files in `/workspace` last
  for the reply. A per-reply VM, rather than one per conversation, keeps cleanup
  deterministic and matches the previous per-reply sandbox state. VMs persist
  if a process crashes, so Vikram records each VM it creates and deletes any
  left by a dead process the next time it starts one.
- **Failing closed.** On a Mac that cannot run VMs, or without a valid image,
  `run_code` is not offered, the prompt omits it, and the reply's notice says
  why; questions and plan mode still work. If a VM fails to start mid-reply,
  the call fails and the model answers without code. Code never runs outside a
  VM.
- **Packaging.** PyInstaller re-signs the binaries it collects, which strips
  the `com.apple.security.hypervisor` entitlement from smolvm's `smol-vmm`
  helper, so the build ships that file verbatim beside smolvm's package, where
  smolvm finds it without environment variables that child processes would
  inherit. smolvm caches its unpacked guest filesystem (46 MB) by the file's
  size and modification time, and a frozen app unpacks files with new times at
  each launch, so Vikram sets that time from the file's content first.
  `--check-sandbox` boots a VM from the frozen sidecar during the build and the
  installer smoke check.

This replaced [Monty](https://github.com/pydantic/monty) through Harness Code
Mode, an interpreter-level sandbox that ran a subset of Python in a worker
process on the Mac.

## 6. Interfaces

**Terminal:** the live reply pauses for each question (numbers choose options,
other text is a typed answer, Enter skips) or plan (y approve, c request changes,
n not now), then resumes. `/plan` or `--plan` turns plan mode on. `/activity`
shows sandbox code and output, answers, and plans. One-shot commands in a
terminal prompt the same way; piped runs decline questions and leave plans
unapproved.

**Desktop:** a waiting question renders as a form, and a waiting plan as an
approval card. Answers return through a validated `respond` command (added to
the Tauri allowlist). The composer has a Plan toggle. Sandbox runs are
collapsible code and output blocks. Library schema v5 adds each chat's mode and
each attempt's mode and activity, written only when activity changes. Waiting
records become stopped if the reply ends without an answer.

**Follow-up context:** completed replies add the questions and answers, the
latest plan and its decision, and up to three recent sandbox runs (bounded) to
later turns, alongside retrieved sources. Failed or stopped replies remain
excluded.

## 7. Evaluation

Three objective cases join the suite: `sandbox-computation`,
`clarifying-question`, and `plan-approval`. A scripted user answers every
question with a fixed typed reply and approves plans. `ToolCheck` passes only if
the required tools completed (a successful run, an answered question, an
approved plan). Tool completion lives only in the in-memory report, which is
discarded; history keeps counts, never prompts, code, plans, or answers.

Evaluations keep web Off. Tool-capable models keep the local toolkit, and each
attempt gets its own VM. Runs record `tools` and `sandbox`, and the Harness and
smolmachines versions join the provenance. Tool cases are refused for models
without tool calling, and the sandbox and plan cases on a Mac that cannot run
the VM. The eight starter cases keep their
suite and protocol fingerprints (pinned by a test), and runs without tools keep
their prompt fingerprint, so existing series stay comparable.
