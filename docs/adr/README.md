# Architecture Decision Records

An ADR is a short note that records one significant decision: what we chose,
why, and what we gave up. The code shows *what* Vikram does; ADRs keep the
*why*, so a later change can revisit a decision knowingly instead of undoing it
by accident.

## When to write one

Write an ADR when a change:

- adopts, replaces or rejects a framework, library or service;
- changes a security boundary (approvals, surfaces, secrets, sandboxing);
- changes how data is stored, traced or evaluated;
- deliberately *doesn't* do the obvious thing (those are the easiest to undo
  by mistake).

A bug fix or a refactor that keeps behaviour needs no ADR.

## How

1. Copy [`template.md`](template.md) to `NNNN-short-title.md`, using the next
   free number.
2. Start at **Proposed**. Set **Accepted** when the change merges.
3. Never rewrite an accepted ADR's decision. To change course, write a new ADR
   and mark the old one **Superseded by NNNN**.
4. Add it to the index below in the same PR.

## Index

| # | Decision | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions as ADRs | Accepted |
| [0002](0002-evals-on-every-commit.md) | Run evals on every relevant commit, locally, with metrics committed | Accepted; triggers superseded by 0011 |
| [0003](0003-opentelemetry-sdk-instead-of-openlit.md) | Trace with the OpenTelemetry SDK instead of OpenLIT | Accepted |
| [0004](0004-adopt-pydantic-ai-harness-declaratively.md) | Adopt Pydantic AI Harness through a declarative `[capabilities]` table | Accepted |
| [0005](0005-file-tools-on-harness-filesystem.md) | Serve file tools from the harness `FileSystem`, behind Vikram's guard | Accepted |
| [0006](0006-keep-load-skill.md) | Keep Vikram's `load_skill` instead of harness `Skills` | Accepted |
| [0007](0007-keep-argv-command-executor.md) | Keep the argv-only command executor; strip secrets from its environment | Accepted |
| [0008](0008-keep-delegate-to-agent.md) | Keep `delegate_to_agent`; forward usage to the parent | Accepted |
| [0009](0009-injection-defence-and-memory-opt-in.md) | Prompt-injection defence in report mode; secret redaction on; memory opt-in | Accepted |
| [0010](0010-durable-agent-runs-opt-in.md) | Durable agent runs on DBOS, off by default | Accepted |
| [0011](0011-eval-triggers-and-pytest-gate.md) | Trigger evals only on model and prompt changes; add `pytest --evals` | Accepted |

0008–0010 were reviewed in raniendu/vikram#43–#45, which merged into stacked
branches; their code reached `main` with raniendu/vikram#47.
