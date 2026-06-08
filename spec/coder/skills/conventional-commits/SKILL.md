---
name: conventional-commits
description: Write a well-formed Conventional Commits message for a set of staged changes. Use when the user asks you to commit, or to write or review a commit message.
---

# Conventional commits

Produce commit messages that follow the Conventional Commits specification so
history stays machine-readable and changelogs can be generated.

## Format

```
<type>(<optional scope>): <description>

<optional body>

<optional footer>
```

- **type**: one of `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`,
  `build`, `ci`, `chore`, `revert`.
- **description**: imperative mood, lower case, no trailing period, <= 72 chars.
- **body**: explain *what* and *why*, not *how*; wrap at 72 columns.
- **footer**: `BREAKING CHANGE: <detail>` for incompatible changes, and any
  `Refs: #123` issue references.

## Method

1. Inspect what is actually staged (`git status`, `git diff --staged`) before
   writing anything. Describe only the staged change.
2. Pick the single `type` that best fits the dominant change. If a change is
   genuinely two things, that is usually a sign it should be two commits.
3. Write the subject line first, then a body only if the change needs
   motivation or context that the diff does not make obvious.
4. Mark breaking changes explicitly in the footer with `BREAKING CHANGE:`.

See `examples.md` (bundled with this skill) for worked examples.
