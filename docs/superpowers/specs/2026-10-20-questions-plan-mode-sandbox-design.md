# Questions, plan mode, and a Python sandbox

Date: October 20, 2026

Status: requested by the owner on October 20, 2026 ("implement Ask User Question
tool, Plan Mode and a Sandbox for code execution using Pydantic related
libraries"), then implemented with CLI/desktop parity, evaluations, and offline
tests. This extends the [documents and web search design](2026-10-14-documents-and-web-search-design.md).

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

- [Pydantic AI Harness](https://pydantic.dev/docs/ai/harness/) `0.54.0`, which
  pins the repository's `pydantic-ai-slim==2.54.0` exactly:
  - `CodeMode(tools=[])` provides `run_code`. With no tools inside, it is a pure
    [Monty](https://github.com/pydantic/monty) sandbox rather than a tool router.
  - `AskUser` provides `ask_user_question`, its schema, validation, and the
    answerer protocol each interface implements.
- `pydantic-monty==1.1.0`: the sandbox interpreter and its worker binary. 1.2.0
  was two days old when chosen, so the older stable release is pinned.
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

Per-reply state (mode, the person, and visible activity records) travels as
the agent's `deps`, so evaluations can reuse one agent across attempts.
A wrapper toolset sits outside CodeMode. That position lets it see `run_code`,
and it decides which tools each request offers:

| Mode | Offered tools |
| --- | --- |
| Normal | web tools (when available), `ask_user_question`, `run_code` |
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
- Each snippet: 15 seconds and 128 MiB. Results reach the model and the UI
  bounded to 8,000 characters; code records to 20,000 and plans to 6,000.
- Sandbox errors return as retries with tracebacks. The fourth consecutive
  failure becomes a finished failure, so a bad snippet never ends the reply.
- Questions and plan reviews wait at most 15 minutes, then continue as
  declined or not approved. Stop/Ctrl+C ends the wait and the reply.

## 5. Sandbox isolation

Monty runs a subset of Python in a separate worker process. It has no
filesystem, network, environment variables, clock, subprocesses, or third-party
packages. Vikram configures no mounts, no OS handler, and no tools inside the
sandbox. Code results are labeled as coming from the model's code, not the
user. REPL state lasts for one reply. The PyInstaller sidecar bundles the
`monty` worker and points `MONTY_BIN` at it.

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

Evaluations keep web Off. Tool-capable models keep the local toolkit; runs
record `tools`, and Harness and Monty versions join the provenance. Tool cases
are refused for models without tool calling. The eight starter cases keep their
suite and protocol fingerprints (pinned by a test), and runs without tools keep
their prompt fingerprint, so existing series stay comparable.
