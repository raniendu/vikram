# Evals: before/after quality tracking

`tests/` proves the code works. `evals/` measures **how well the agents
answer**, and how that changes when you change a prompt, a model, a tool or a
framework version. Evals run in two ways:

| When | What runs | Result |
|---|---|---|
| A commit changes a **model, model settings, model version, framework version or prompt** | post-commit hook queues a before/after run in the background | committed to `evals/history/`, tracked over time |
| You run **`uv run pytest --evals`** | the tests, then the suite on your working tree, compared with the last recorded result | fails if any case got worse; nothing committed |

Other changes (tools, MCP, hooks, skills, capabilities, eval cases) don't start
a run on their own, but are still listed in the next run's "what changed".

Evals are **not** part of CI or the Docker image (`.dockerignore` excludes
`evals/`). They run on your machine, against your local Ollama models.

## Setup (once)

```bash
uv sync --locked
uv run pre-commit install      # installs the pre-commit AND post-commit hooks
```

Ollama must be running with each spec's model pulled (`qwen3.6:35b-mlx` for
`coder`, `gemma4:26b-a4b-it-qat` for `vikram` and the judge).

## What happens when you commit

```
git commit
  └─ post-commit hook (under a second, never blocks or fails the commit)
       ├─ diff HEAD~1..HEAD → which change kinds, which agents?
       └─ no triggering kind → stop
          triggering kind    → queue a job, start a background worker
worker (.vikram/evals/logs/worker.log)
  ├─ baseline = the agent's latest committed record on an ancestor commit
  │             with the same suite; if none, run the suite on HEAD~1 first
  ├─ run the suite on HEAD (in a git worktree of that exact commit)
  ├─ write evals/history/YYYY/MM/<time>_<sha>_<agent>.json
  └─ commit only that file: "evals: coder results for ab12cd3 (prompt)"
```

- Suites run in **git worktrees** of the commit under test, each in its own
  `uv` environment, so uncommitted edits and branch switches never leak into a
  result. Both sides of a comparison run the **same** cases (HEAD's suite).
- A results commit touches only `evals/history/` and leaves anything else you
  have staged alone. It is skipped (file left for `python -m evals record`)
  during a merge or rebase, on a detached HEAD, or if you switched branch.
- One worker at a time. Commits made while it runs are coalesced into one
  follow-up job; the diff from baseline to the newest HEAD covers them all.
- If Ollama is not reachable, the job is skipped and nothing is recorded.
- Every step has a time limit, so a hung model server can't stall the worker:
  - each agent run is limited to its case's `timeout_seconds` (default 600);
  - each judge call to `VIKRAM_EVAL_JUDGE_TIMEOUT` seconds (default 120);
  - the whole suite run to the sum of its case limits plus room for checks and
    setup (`VIKRAM_EVALS_SUITE_TIMEOUT` overrides it). A suite past its limit is
    killed along with everything it started, and the job is marked `FAILED`
    in `status`.

To see what a commit changed and whether it would trigger a run, without
running anything:

```bash
uv run python -m evals detect                       # HEAD~1..HEAD
uv run python -m evals detect --base main --head HEAD
```

### Change kinds

| Kind | Triggered by | Recorded detail |
|---|---|---|
| `prompt` | `spec/*/system_prompt.md`, `spec/shared/context/**`, `vikram/context.py`, `context_files` in a spec | lines added/removed |
| `model` | `model` / `model_provider` in `spec/*/agent.toml`, `vikram/providers.py`, `vikram/model_catalog.py` | from → to |
| `model_settings` | any key in `[model_settings]` | each key from → to |
| `model_version` | Ollama digest differs from the baseline's (same tag, re-pulled) | digest from → to |
| `tools` | `vikram/tools.py`, `vikram/command_policy.py`, command policy TOML, `tools = [...]` | tools added/removed |
| `mcp_hooks_skills` | `[[mcp_servers]]`, `[[hooks]]`, skills, `SKILL.md`, `mcp.py`, `hooks.py`, `skills.py`, `delegation.py` | names added/removed (never URLs or env) |
| `capabilities` | `[capabilities]` in `spec/*/agent.toml`, `vikram/capabilities.py` | each capability from → to |
| `framework` | `pydantic-ai-slim` / `pydantic-ai-harness` version in `uv.lock`, `vikram/agent.py` | version from → to |
| `eval_suite` | `evals/cases/**`, `evals/fixtures/**`, `evals/checks.py`, `evals/judge.py` | new suite hash |

`spec/coder/**` affects only `coder`; shared files and Vikram code affect every
agent. A model re-pulled under the same tag changes no file, so run
`uv run python -m evals check-models` (by hand or from cron) to catch it.

### Which kinds start a run

By default only `model`, `model_settings`, `model_version`, `framework` and
`prompt` start a run from the hook. Every kind is still detected and recorded
when a run happens. Change the set with `VIKRAM_EVALS_TRIGGERS`:

```bash
VIKRAM_EVALS_TRIGGERS=all                 # every kind, as before
VIKRAM_EVALS_TRIGGERS=model,model_version # only model swaps and re-pulls
```

An unknown kind is an error, and the hook then queues nothing.
`uv run python -m evals detect` shows the kinds a commit changed, the active
triggers, and whether the hook would queue a run.

## Before you push: `pytest --evals`

```bash
uv run pytest --evals                                        # all agents, 3 repeats
uv run pytest --evals --eval-agent coder --eval-repeats 1    # quicker
```

1. The normal tests run first. If any fails, the evals are skipped.
2. Each agent's suite runs on the **working tree** (uncommitted edits
   included), with the usual progress lines.
3. The result is compared with the newest recorded result on `HEAD` or an
   ancestor that used the same eval cases, and the before/after table is printed.
4. The session **fails** if any case got worse beyond the noise margins (see
   "How scoring works"), or if the model server isn't reachable.

Nothing is committed. The result is kept as a manual run, so
`uv run python -m evals compare pytest-coder <sha> --agent coder` shows it again.
If there is no recorded result yet, the run passes and says so.

Plain `uv run pytest` (and CI) never runs evals: tests stay offline and fast.

## Watching a run

A job prints one line per step: to the terminal with `enqueue --wait`, and to
`.vikram/evals/logs/worker.log` when the hook started it in the background.

```
[evals 14:02:10] job 3a1b2c4: 4 suite runs for coder, vikram (no earlier results to compare with, so the parent commit is scored first)
[evals 14:02:10] step 1/4: coder, before (9f8e7d6). Preparing the commit's Python environment; ...
[evals 14:03:40] coder: 12 cases x 3 repeats = 36 agent runs
[evals 14:03:40] case 1/12 coder.fix_failing_test, repeat 1/3: running (0/36 runs done)
[evals 14:04:21] case 1/12 coder.fix_failing_test, repeat 1/3: passed (41s, judge 0.90). 1/36 runs done, about 23m 55s left
```

From another terminal, `status` shows where the job is:

```
$ uv run python -m evals status
queued: nothing
running: job 3a1b2c4 for coder, vikram, 3 repeats, 14m 10s so far
  step 2/4: coder, after (3a1b2c4)
  case 5/12 coder.edit_readme, repeat 2/3
  14/36 runs done, 12 passed, about 21m 00s left in this step
```

- A **step** is one suite run on one commit: `before` (the parent commit,
  scored only when there is no earlier result to compare with) or `after`.
- **Time left** is the average time per finished run times the runs left in
  the step. It appears after the first run.
- If the worker died, `status` says the job stopped without finishing.
- Lines carry case ids, pass/fail, judge scores and times, never prompts or
  model output (those stay in `.vikram/evals/runs/`).
- The agent under test logs at `WARNING` during a suite, so its own messages
  don't bury the progress. Set `VIKRAM_EVALS_LOG_LEVEL=INFO` to see them.

## Reading results

```bash
uv run python -m evals status                    # progress, queue, last results
uv run python -m evals compare 9f8e7d6 ab12cd3 --agent coder
uv run python -m evals report                    # .vikram/evals/report.html
```

`compare` accepts commit shas, run ids, manual run labels or JSON paths, and
lists the cases that got worse first. The report shows, per agent, pass rate
and judge score over time (hover a point for the commit and the change), and
a table of runs with what changed and a per-case breakdown.

Full outputs and tool-call traces for every run stay local, in
`.vikram/evals/runs/<run_id>/cases/`, for digging into a failure.

### What is recorded

Each record holds: the commit, the change kinds and details, the model and its
Ollama digest, the judge model, package versions, the suite hash, the summary
(pass rate, judge score, tokens, latency, tool calls) and per-case metrics with
the names of failed checks, plus the difference from the baseline. It never
holds prompts, answers or author emails.

Records are about 10 KB each (1,000 runs ≈ 10 MB) and are never pruned. The
history is the point: it lets you see quality over time.

### How scoring works

Each case runs **3 times** (`VIKRAM_EVALS_REPEATS` to change) because local
models vary. A repeat passes when all its hard checks pass and, for judged
cases, the judge score reaches the case threshold (default 0.7). A case's
**pass rate** is the share of passing repeats.

A case is marked **worse** or **better** only when its pass rate moves by at
least 0.34 (one flipped repeat out of three is treated as noise) or its judge
score moves by at least 0.15.

- **Hard checks** (`evals/checks.py`): the output contains a value, a tool was
  (or was not) called, a file changed, the fixture's tests pass, a hidden
  test passes, nothing outside the allowed files changed.
- **LLM judge** (`evals/judge.py`): scores an answer 0–1 against a written
  rubric at temperature 0. It defaults to the `vikram` spec's model; set
  `VIKRAM_EVAL_JUDGE_PROVIDER` and `VIKRAM_EVAL_JUDGE_MODEL` to use another.

Evals measure the specs **as committed**: `VIKRAM_MODEL*` overrides and saved
`/model` choices are ignored. Unattended approvals allow file edits,
delegation and test runs (`python`, `pytest`) inside a throwaway copy of the
fixture, and deny every other approval-gated command.

## Adding a case

Add an entry to `evals/cases/<agent>.yaml`:

```yaml
  - id: coder.my_new_case          # <agent>.<name>; used in history, keep stable
    workspace: mini_repo           # copied fresh from evals/fixtures/ per repeat
    surface: cli                   # cli | threaded | ...
    prompt: Explain what inventory/stock.py does.
    checks:
      - {type: tool_called, tool: read_file}
      - {type: output_contains, value: compute_total}
    judge:                         # optional
      rubric: Mentions the Item dataclass and the total value calculation.
      threshold: 0.7
    tags: [needs_web]              # optional: skipped without PARALLEL_API_KEY
```

Editing cases, fixtures or scoring code changes the suite hash. The next run
then builds a fresh baseline on the parent commit, because results from
different suites are never compared.

## Running by hand

```bash
uv run python -m evals run --agent coder --label try1 --repeats 1   # working tree, not committed
uv run python -m evals compare try1 ab12cd3 --agent coder
uv run python -m evals enqueue --agent coder --wait                 # before/after for HEAD, committed
uv run python -m evals record                                       # commit leftover results
```

## Turning it off

- `VIKRAM_EVALS_DISABLE=1`: the hook does nothing.
- `VIKRAM_EVALS_TRIGGERS=<kinds>`: narrow or widen what starts a run.
- `VIKRAM_EVALS_AUTOCOMMIT=0`: results are written but not committed.
- `uv run pre-commit uninstall -t post-commit`: remove the hook.

The worker needs a POSIX system (it uses `fcntl` file locks) and `uv` on `PATH`.
