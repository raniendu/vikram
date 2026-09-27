# 0002. Run evals on every relevant commit, locally, with metrics committed

- **Status:** Accepted; which changes trigger a run is superseded by [0011](0011-eval-triggers-and-pytest-gate.md)
- **Date:** 2026-09-26
- **PR:** raniendu/vikram#36 (progress reporting: raniendu/vikram#46)

## Context

Vikram's quality depends on prompts, models, model versions, tools, MCP
servers, hooks, skills and the framework version. A change to any of them can
make answers worse while every unit test still passes. There was no way to
compare an agent's behaviour before and after a change, or to see a trend.

The shipped agents run on local Ollama models, so CI runners can't run them.

## Options considered

| Option | For | Against |
|---|---|---|
| Evals in CI | Runs for every PR automatically | No Ollama model on CI runners; a hosted model measures a different agent |
| Manual evals before a release | Simple | Easy to forget; no history; can't say which change caused a drop |
| **Post-commit hook, local worker** | Uses the real local model; runs only when a relevant file changed; never blocks a commit | Needs the developer's machine and Ollama; results arrive minutes to hours later |

## Decision

A post-commit hook (`uv run pre-commit install`) looks at what the commit
changed. If it touched something that affects an agent, it queues a job for
a background worker. The hook itself takes under a second.

- Change kinds are detected from the diff: `prompt`, `model`,
  `model_settings`, `model_version`, `tools`, `mcp_hooks_skills`,
  `framework`, `capabilities`, `eval_suite`.
- The suite runs in a **git worktree of the exact commit**, with that commit's
  locked environment, so uncommitted edits never leak into a result.
- The baseline is the agent's latest committed result on an ancestor commit,
  or the parent commit is scored first.
- Grading is hard checks plus an LLM judge (`pydantic-evals`), repeated 3 times.
- Only metrics (pass rates, scores, tokens, latency) go to `evals/history/` and
  are committed. Prompts, outputs and traces stay in `.vikram/`.

See `docs/evals.md`.

## Consequences

- Every relevant change gets a before/after score, and the history shows trends.
- A first run is slow (both commits, 3 repeats). Progress lines and
  `python -m evals status` show how far along it is.
- Results depend on the developer's hardware and model pulls; `check-models`
  re-runs when an Ollama model digest changes.
- The repo gains small metrics commits (`evals: ... results for ...`).

## Revisit when

A hosted model becomes the default, or a CI runner can serve the local model.
Then the same suite can run in CI.
