# Conventional commit examples

A feature with a scope:

```
feat(api): add /threads pagination cursor

Return a `next_cursor` field so clients can page through long threads
without re-fetching the whole history.
```

A bug fix referencing an issue:

```
fix(telegram): drop duplicate updates by update_id

Refs: #142
```

A breaking change:

```
refactor(config): require explicit model provider

BREAKING CHANGE: VIKRAM_MODEL_PROVIDER must now be set; there is no
default provider.
```

A chore with no body (the subject is self-explanatory):

```
chore: bump strands-agents to 1.44.0
```
