# Evals: before/after quality tracking

`tests/` proves the code works. `evals/` measures **how well the agents
answer**, and how that changes when you change a prompt, a model, a tool or a
framework version. Every relevant commit gets a before/after run, and the
results are committed to `evals/history/`, so quality can be tracked over time.

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
       └─ nothing relevant → stop
          relevant         → queue a job, start a background worker
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

## Reading results

```bash
uv run python -m evals status                    # queue + last results
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
- `VIKRAM_EVALS_AUTOCOMMIT=0`: results are written but not committed.
- `uv run pre-commit uninstall -t post-commit`: remove the hook.

The worker needs a POSIX system (it uses `fcntl` file locks) and `uv` on `PATH`.
