# Repository Guidelines

## Scope

Vikram is one local assistant built with Pydantic AI and Ollama. Keep the
implementation small. Do not introduce additional agents, agent registries,
tools, delegation, integrations, server surfaces, persistence, or configuration
frameworks unless explicitly requested.

## Structure

- `vikram/agent.py`: the single agent definition, inline instructions, default
  model, and `build_agent(model: str = DEFAULT_MODEL) -> Agent`.
- `vikram/cli.py`: interactive and one-shot command-line conversations.
- `vikram/__main__.py`: entry point for `python -m vikram`.
- `vikram/evals/`: manually invoked Pydantic Evals, local Ollama rubric grading,
  metrics-only history, and a self-contained HTML report.

The model endpoint is fixed to `http://localhost:11434/v1`. Model selection is
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
- `uv run vikram --help`: check CLI startup without calling Ollama.
- `uv run vikram-eval run`: benchmark the eight cases, five attempts per thinking
  level advertised by Ollama. Repeat `--model` to benchmark multiple local models.
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

## Working Style

Inspect relevant files first and prefer small, maintainable changes. Preserve
unrelated local work. Do not commit, push, open pull requests, or delete user
state unless explicitly asked.

Use Python 3.13 or later, type hints at public boundaries, Black's 88-character
line length, and isort's Black profile. Keep documentation consistent with the
code.

Do not add a test suite or a separate agent-spec system unless requested.
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
levels, pool different levels, or store thinking traces. Explicit defaults and
untested supported levels must remain distinguishable from scored results.
