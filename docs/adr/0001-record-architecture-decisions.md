# 0001. Record architecture decisions as ADRs

- **Status:** Accepted
- **Date:** 2026-09-27
- **PR:** this ADR's PR

## Context

The Pydantic AI Harness migration made several decisions in a row, some of
them to *not* adopt a harness feature. The reasons lived in PR descriptions and
in "Decision" sections of `docs/capabilities.md`. PR text is hard to find later,
and a reference page is the wrong place for history: it should describe what is
true now.

## Decision

Keep one short Markdown file per significant decision in `docs/adr/`, numbered
in order, using [`template.md`](template.md). Reference docs (`docs/*.md`) keep
describing current behaviour and link to the ADR for the reasoning.

## Consequences

- A reviewer can see why something is the way it is before changing it.
- Decisions that say "no" are recorded, so they aren't retried blindly.
- One more file per significant PR. The bar in [README.md](README.md) keeps
  that to real decisions.

## Revisit when

The ADRs stop being read or updated; then fold them back into the docs.
