# Threaded Conversations

The threaded surface gives each interface-native thread durable message history.

```bash
uv run vikram-api
curl http://127.0.0.1:8000/threads/web/demo/messages \
  --json '{"prompt":"remember that this thread is a demo"}'
curl http://127.0.0.1:8000/events/<workflow_id>
```

`ThreadStore` persists rows keyed by `(interface, external_thread_id)` in
`.vikram/vikram.sqlite3`. DBOS workflow state lives in `.vikram/dbos.sqlite3`
unless `DBOS_SYSTEM_DATABASE_URL` overrides it.

## Durable agent runs

Each inbound message is a DBOS workflow, so a crash never loses a message.
Pydantic AI's `DBOSDurability` capability is also added to threaded agents, so
every model request, every MCP call and the compaction summary are **DBOS
steps**: a restarted workflow replays the finished ones from the journal and
continues from where it stopped, instead of redoing the whole agent run.

- It's **on by default** ([ADR 0012](adr/0012-durable-agent-runs-on-by-default.md)).
  Turn it off with `VIKRAM_DURABLE_AGENT_RUNS=false`; no migration is needed
  either way.
- Only model requests, MCP I/O and capability operations are checkpointed.
  Plain function tools (`web_search`, `delegate_to_agent`) run again on
  replay, so their results can differ from the first attempt.
- Outside a workflow (CLI, ACP, `/chat`) the capability does nothing.
- Deploy upgrades carefully when runs are in flight. A new release that
  changes an agent's steps can't resume a run journaled by the old one.

Use `/reset` from Telegram to clear a thread's message history. The selected
agent can be changed with `/agent <name>` when that spec is allowed on Telegram.
