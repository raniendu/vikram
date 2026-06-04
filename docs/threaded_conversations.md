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

Use `/reset` from Telegram to clear a thread's message history. The selected
agent can be changed with `/agent <name>` when that spec is allowed on Telegram.
