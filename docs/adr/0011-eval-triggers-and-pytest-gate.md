# 0011. Trigger evals only on model and prompt changes; add `pytest --evals`

- **Status:** Accepted
- **Date:** 2026-09-27
- **PR:** this ADR's PR
- **Supersedes:** the trigger rule in [0002](0002-evals-on-every-commit.md)

## Context

[0002](0002-evals-on-every-commit.md) started an eval run for any of nine change
kinds. Most code commits touch one of them (tools, MCP, hooks, skills,
capabilities, eval cases), so in practice a run followed almost every commit.
A full run takes a long time on a local model. There was also no way to check
the working tree before committing, other than running `python -m evals run`
and comparing by hand.

## Options considered

| Option | For | Against |
|---|---|---|
| Keep all nine kinds | Nothing missed | A long background run after nearly every commit |
| Trigger only on model kinds | Fewest runs | Framework upgrades and prompt edits change answers too, and would go unmeasured |
| **Model kinds + framework + prompt, configurable; plus an opt-in pytest gate** | Runs where answers are most likely to move; the rest can still be checked on demand | Tool or skill regressions are caught only if someone runs `pytest --evals` or widens the triggers |
| Run evals in every `pytest` | Always checked | Breaks offline, fast tests and CI (no Ollama there) |

## Decision

- The hook starts a run only for `model`, `model_settings`, `model_version`,
  `framework` and `prompt`. `VIKRAM_EVALS_TRIGGERS` changes the set (`all`, or a
  comma-separated list). Every kind is still detected and recorded when a run
  happens.
- `uv run pytest --evals` runs the tests, then each agent's suite on the working
  tree, compares it with the newest recorded result on `HEAD` or an ancestor
  (same eval cases), and fails the session if any case got worse beyond the
  noise margins or the model server is unreachable. Nothing is committed.
- Plain `pytest` and CI never run evals.

## Consequences

- Far fewer background runs; history still records every model, framework and
  prompt change.
- Tool, MCP, hook, skill and capability changes need `pytest --evals` (or
  `enqueue --wait`) to be measured.
- `pytest --evals` is only as good as the last recorded result: with no
  history yet, it reports scores and passes.

## Revisit when

A regression slips through in a kind that isn't a trigger: add that kind to
the defaults.
