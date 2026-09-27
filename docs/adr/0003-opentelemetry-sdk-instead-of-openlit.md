# 0003. Trace with the OpenTelemetry SDK instead of OpenLIT

- **Status:** Accepted
- **Date:** 2026-09-26
- **PR:** raniendu/vikram#37 (WS8)

## Context

Vikram used OpenLIT to trace model calls. The harness migration needs
`pydantic-ai-slim>=2.44`, which needs `anthropic>=1.0`. Every OpenLIT release
pins `anthropic<1.0`, so the two can't be installed together.

## Options considered

| Option | For | Against |
|---|---|---|
| Keep OpenLIT, skip the harness | No tracing change | Blocks the whole migration |
| Wait for an OpenLIT release | No code change | No date; blocks the migration indefinitely |
| **OpenTelemetry SDK + Pydantic AI's own instrumentation** | No version pin conflict; Pydantic AI already emits GenAI spans; standard OTLP export | Lose OpenLIT's auto-instrumentation of other libraries |

## Decision

Remove OpenLIT. Install the OpenTelemetry SDK with OTLP/HTTP exporters for
traces and metrics. Model requests, tool calls and token usage come from
Pydantic AI's built-in instrumentation. `VIKRAM_OTLP_ENDPOINT` still sets the
collector; `VIKRAM_OBSERVABILITY_DISABLED_INSTRUMENTORS` is accepted but ignored,
with a warning.

## Consequences

- The harness upgrade can proceed.
- Traces keep the same shape: one trace from HTTP request to DBOS workflow,
  with `trace_id`/`span_id` on log lines.
- Libraries OpenLIT used to instrument automatically no longer are. Add an
  instrumentor explicitly if one is needed.

## Revisit when

Only if Pydantic AI's instrumentation stops covering something we need.
