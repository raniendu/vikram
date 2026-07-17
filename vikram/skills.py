"""Agent Skills support for Vikram agents.

A *skill* is a folder of expert instructions for a specific kind of task,
following the Anthropic Agent Skills convention: a ``SKILL.md`` file with a
small YAML-style frontmatter block (``name`` and ``description``) followed by a
Markdown body, optionally accompanied by bundled resource files in the same
directory.

Agents reference skills in ``agent.toml`` via ``skills`` (paths relative to the
agent's own spec directory) and ``shared_skills`` (paths relative to
``spec/shared``). Each path may point at a skill directory (containing
``SKILL.md``) or directly at a Markdown file.

Skills use progressive disclosure: only each skill's name and one-line
description are placed in the agent's instructions (see
:func:`skills_instructions`). The agent loads a skill's full body on demand
through the ``load_skill`` tool built by :func:`make_load_skill_tool`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel
from pydantic_ai import Tool

if TYPE_CHECKING:
    from vikram.spec import AgentSpec

SKILL_FILENAME = "SKILL.md"
# Cap on bundled resources listed for a single skill, to bound tool output.
MAX_SKILL_RESOURCES = 50


class SkillError(RuntimeError):
    """Raised when a skill cannot be located, parsed, or is misconfigured."""


class Skill(BaseModel):
    """A loaded skill: its metadata, full instructions, and bundled resources."""

    name: str
    description: str
    instructions: str
    source: Path
    resources: list[str] = []


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a ``---``-delimited frontmatter block from the Markdown body.

    Frontmatter keys are parsed with a first-colon split (so a value may itself
    contain colons), lower-cased, and de-quoted. If the text has no opening
    ``---`` fence, or no closing fence, the whole text is treated as the body
    with empty metadata.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()

    meta: dict[str, str] = {}
    body_start: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            body_start = index + 1
            break
        raw = lines[index]
        if not raw.strip() or raw.lstrip().startswith("#") or ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        meta[key.strip().lower()] = _strip_quotes(value.strip())

    if body_start is None:
        return {}, text.strip()
    return meta, "\n".join(lines[body_start:]).strip()


def load_skill(path: Path) -> Skill:
    """Load a single skill from a directory or a Markdown file.

    A directory must contain a ``SKILL.md``. The skill's bundled resources are
    every other file in the skill directory (recursively), reported as paths
    relative to that directory.
    """
    if path.is_dir():
        skill_dir = path
        skill_file = path / SKILL_FILENAME
        if not skill_file.is_file():
            raise SkillError(
                f"Skill directory {path} is missing a {SKILL_FILENAME} file."
            )
    elif path.is_file():
        skill_file = path
        skill_dir = path.parent
    else:
        raise SkillError(f"Skill path does not exist: {path}")

    try:
        text = skill_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillError(f"Could not read skill {skill_file}: {exc}") from exc

    meta, body = _parse_frontmatter(text)
    name = meta.get("name") or skill_dir.name
    description = meta.get("description", "")
    if not description:
        raise SkillError(
            f"Skill {name!r} ({skill_file}) is missing a 'description' in its "
            f"frontmatter; it is required for progressive disclosure."
        )
    if not body:
        raise SkillError(f"Skill {name!r} ({skill_file}) has no instructions body.")

    resources: list[str] = []
    for item in sorted(skill_dir.rglob("*")):
        if item.is_file() and item != skill_file:
            resources.append(item.relative_to(skill_dir).as_posix())
            if len(resources) >= MAX_SKILL_RESOURCES:
                break

    return Skill(
        name=name,
        description=description,
        instructions=body,
        source=skill_file,
        resources=resources,
    )


def discover_skills(spec: AgentSpec) -> list[Skill]:
    """Load every skill configured on ``spec``, rejecting duplicate names.

    Agent-local ``skills`` resolve against the agent's spec directory; shared
    ``shared_skills`` resolve against ``spec/shared``. Local skills are loaded
    first so they take precedence in the listed order.
    """
    entries: list[tuple[Path, Path]] = [
        (spec.agent_dir, rel) for rel in spec.skills
    ] + [(spec.shared_dir, rel) for rel in spec.shared_skills]

    skills: list[Skill] = []
    seen: set[str] = set()
    for base, rel in entries:
        skill = load_skill(base / rel)
        if skill.name in seen:
            raise SkillError(
                f"Duplicate skill name {skill.name!r}; skill names must be "
                f"unique within an agent."
            )
        seen.add(skill.name)
        skills.append(skill)
    return skills


def skills_instructions(skills: list[Skill]) -> str:
    """Build the progressive-disclosure instruction block for ``skills``.

    Returns an empty string when there are no skills so callers can append
    unconditionally.
    """
    if not skills:
        return ""

    lines = [
        "## Available skills",
        "",
        "You have access to the skills listed below. Each skill is a set of "
        "expert instructions for a specific kind of task; the summaries here "
        "are deliberately brief.",
        "",
        "When a request matches a skill, call the `load_skill` tool with the "
        "skill's exact name to read its full instructions before you act. Do "
        "not infer a skill's contents from its summary, and do not mention "
        "this mechanism to the user.",
        "",
    ]
    lines.extend(f"- **{skill.name}**: {skill.description}" for skill in skills)
    return "\n".join(lines)


def render_skill(skill: Skill) -> str:
    """Render a skill's full instructions plus any bundled-resource listing."""
    parts = [f"# Skill: {skill.name}", "", skill.instructions.strip()]
    if skill.resources:
        listing = "\n".join(f"- {resource}" for resource in skill.resources)
        parts += [
            "",
            "## Bundled resources",
            "",
            "This skill ships the following files alongside it, with paths "
            "relative to the skill's directory. Read them with your file tools "
            "if the instructions above call for them:",
            "",
            listing,
        ]
    return "\n".join(parts).strip()


def make_load_skill_tool(skills: list[Skill]) -> Tool[None]:
    """Build the ``load_skill`` tool bound to ``skills`` for one agent.

    The returned tool is added to the agent only when it has at least one
    skill, so its presence signals that skills exist.
    """
    index = {skill.name: skill for skill in skills}

    async def load_skill_tool(name: str) -> str:
        """Load the full instructions for one of your available skills.

        Call this with the exact skill name shown under "Available skills" when
        a user request matches that skill, before you act on it. Returns the
        skill's complete instructions and a list of any bundled resource files.

        Args:
            name: The exact name of the skill to load.
        """
        skill = index.get(name)
        if skill is None:
            available = ", ".join(sorted(index)) or "(none)"
            return f"Unknown skill {name!r}. Available skills: {available}."
        return render_skill(skill)

    return Tool(load_skill_tool, name="load_skill")
