# 0012. Durable agent runs on by default

- **Status:** Accepted
- **Date:** 2026-09-27
- **PR:** this ADR's PR
- **Supersedes:** the "off by default" part of [0010](0010-durable-agent-runs-opt-in.md)

## Context

[0010](0010-durable-agent-runs-opt-in.md) added `DBOSDurability` to threaded
agents behind `VIKRAM_DURABLE_AGENT_RUNS`, off by default until it had run in a
deployment. Vikram's deployments are personal: one owner, one bot, and nobody
else to disrupt. A separate staging copy adds more setup than it saves. Turning
the flag off again is a restart, with no data migration.

## Options considered

| Option | For | Against |
|---|---|---|
| Keep it off; turn it on per deployment | Safest | Every deployment has to know the flag exists; crashes keep redoing whole runs |
| Stage it first, then default on | Proven before anyone relies on it | A second bot and deployment to run for a single-owner project |
| **On by default, opt out with `=false`** | Every threaded deployment resumes instead of redoing runs | The known edges (below) reach every deployment at once |

## Decision

`VIKRAM_DURABLE_AGENT_RUNS` defaults to `true`. `build_agent` still defaults to
not durable; only threaded conversations pass the setting, so the CLI, ACP,
GUI and `/chat` are unchanged.

## Consequences

- A restart mid-reply resumes from the last finished model call, saving time
  and tokens and keeping the answer consistent.
- Plain function tools (`web_search`, `delegate_to_agent`) run again on
  replay (unchanged from 0010).
- A release that changes an agent's steps can't resume a run the previous
  release left half-finished: deploy while idle when possible.
- Opting out is `VIKRAM_DURABLE_AGENT_RUNS=false` and a restart.

## Revisit when

A resumed run misbehaves (duplicate or missing reply, replay error): turn it
off with the env var, and record what happened here.
