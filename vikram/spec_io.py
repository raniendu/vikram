"""Read and write ``agent.toml`` while preserving what a human wrote there.

Agent specs are documentation as much as configuration -- the shipped ones are
mostly commented-out ``[[mcp_servers]]`` templates and annotated
``[model_settings]`` knobs, and users copy that habit into their own. An editor
that round-trips a spec must not silently delete those comments, so this module
uses tomlkit rather than re-emitting from the parsed model.

``config.py`` keeps its own hand-rolled emitter for ``config.toml``: that file
has no arrays-of-tables, its emitter is well covered, and it holds API keys.
Agent-spec concerns stay away from it deliberately.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit import TOMLDocument
from tomlkit.items import Table

from vikram.spec import AgentSpecDraft

# Order used when writing a spec from scratch. Mirrors the shipped specs so a
# GUI-authored file reads like a hand-written one.
KEY_ORDER = (
    "name",
    "description",
    "system_prompt",
    "cli_only",
    "model_provider",
    "model",
    "context_files",
    "skills",
    "shared_context_files",
    "shared_skills",
    "tools",
    "command_policy",
)

# Emitted as their own tables/arrays-of-tables, after the scalars above.
_TABLE_KEYS = ("model_settings", "command_policy_override")
_AOT_KEYS = ("mcp_servers", "hooks")


class SpecWriteError(RuntimeError):
    """Raised when a spec cannot be parsed or serialised."""


def _plain(value: Any) -> Any:
    """Convert pydantic values into something tomlkit can emit."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def _draft_fields(draft: AgentSpecDraft) -> dict[str, Any]:
    """Field values, with unset optionals and empty collections dropped.

    Writing ``skills = []`` for every agent would be noise; omitting it lets the
    model default apply and keeps generated files readable.
    """
    dumped = draft.model_dump(exclude_none=True)
    return {
        key: _plain(value)
        for key, value in dumped.items()
        if not (isinstance(value, (list, dict)) and not value)
    }


def _sync_table(doc: TOMLDocument | Table, key: str, value: dict[str, Any]) -> None:
    existing = doc.get(key)
    if isinstance(existing, Table):
        for subkey in [k for k in existing.keys() if k not in value]:
            del existing[subkey]
        for subkey, subvalue in value.items():
            existing[subkey] = subvalue
        return
    table = tomlkit.table()
    for subkey, subvalue in value.items():
        table[subkey] = subvalue
    doc[key] = table


def _sync_aot(doc: TOMLDocument, key: str, entries: list[dict[str, Any]]) -> None:
    """Replace an array-of-tables wholesale.

    Entry-level comment preservation would require matching old entries to new
    ones by identity, which there is no stable key for once a user reorders
    them in the editor. Replacing is predictable; the commented-out template
    blocks that live *outside* the AoT are untouched either way.
    """
    if not entries:
        if key in doc:
            del doc[key]
        return
    aot = tomlkit.aot()
    for entry in entries:
        table = tomlkit.table()
        for subkey, subvalue in entry.items():
            table[subkey] = subvalue
        aot.append(table)
    doc[key] = aot


def render_agent_toml(draft: AgentSpecDraft, *, existing: str | None = None) -> str:
    """Serialise ``draft`` to TOML text.

    When ``existing`` is given, its comments, key order and formatting are
    preserved; keys absent from the draft are removed, and new keys are
    appended. Pure: takes text, returns text, touches no filesystem.
    """
    try:
        doc = tomlkit.parse(existing) if existing else tomlkit.document()
    except Exception as exc:  # tomlkit raises several parse error types
        raise SpecWriteError(f"Could not parse existing spec: {exc}") from exc

    fields = _draft_fields(draft)
    scalars = {k: v for k, v in fields.items() if k not in _TABLE_KEYS + _AOT_KEYS}

    for key in [k for k in list(doc.keys()) if k not in fields]:
        del doc[key]

    ordered = [k for k in KEY_ORDER if k in scalars]
    ordered += [k for k in scalars if k not in KEY_ORDER]
    for key in ordered:
        doc[key] = scalars[key]

    for key in _TABLE_KEYS:
        if key in fields:
            _sync_table(doc, key, fields[key])
    for key in _AOT_KEYS:
        _sync_aot(doc, key, fields.get(key, []))

    return tomlkit.dumps(doc)


def read_agent_toml(path: Path) -> tuple[dict[str, Any], str]:
    """Return ``(parsed data, raw text)`` for an ``agent.toml``."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecWriteError(f"Could not read {path}: {exc}") from exc
    try:
        return tomlkit.parse(text).unwrap(), text
    except Exception as exc:
        raise SpecWriteError(f"Invalid TOML in {path}: {exc}") from exc


def write_agent_toml(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(text)
    os.replace(tmp_path, path)
