from pathlib import Path

import pytest

from vikram.skills import (
    Skill,
    SkillError,
    _parse_frontmatter,
    discover_skills,
    load_skill,
    make_load_skill_tool,
    render_skill,
    skills_instructions,
)
from vikram.spec import AgentSpec


def _make_spec(tmp_path, *, skills=(), shared_skills=()) -> AgentSpec:
    return AgentSpec(
        name="demo",
        description="demo",
        system_prompt=Path("system_prompt.md"),
        agent_dir=tmp_path,
        shared_dir=tmp_path / "shared",
        skills=[Path(p) for p in skills],
        shared_skills=[Path(p) for p in shared_skills],
    )


def _write_skill(directory: Path, name: str, description: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return directory


def test_parse_frontmatter_basic():
    meta, body = _parse_frontmatter(
        "---\nname: foo\ndescription: Use when X happens: do Y\n---\n\nBody text"
    )
    assert meta == {"name": "foo", "description": "Use when X happens: do Y"}
    assert body == "Body text"


def test_parse_frontmatter_strips_quotes():
    meta, _ = _parse_frontmatter('---\nname: "quoted"\n---\nbody')
    assert meta["name"] == "quoted"


def test_parse_frontmatter_without_fence_is_all_body():
    meta, body = _parse_frontmatter("# Just markdown\n\nNo frontmatter here.")
    assert meta == {}
    assert body == "# Just markdown\n\nNo frontmatter here."


def test_parse_frontmatter_unclosed_fence_is_all_body():
    text = "---\nname: foo\nstill open"
    meta, body = _parse_frontmatter(text)
    assert meta == {}
    assert body == text


def test_load_skill_from_directory_collects_resources(tmp_path):
    skill_dir = _write_skill(
        tmp_path / "pdf", "pdf", "Work with PDFs.", "Detailed PDF instructions."
    )
    (skill_dir / "helper.py").write_text("print('hi')", encoding="utf-8")
    (skill_dir / "refs").mkdir()
    (skill_dir / "refs" / "spec.md").write_text("ref", encoding="utf-8")

    skill = load_skill(skill_dir)

    assert skill.name == "pdf"
    assert skill.description == "Work with PDFs."
    assert skill.instructions == "Detailed PDF instructions."
    assert skill.resources == ["helper.py", "refs/spec.md"]


def test_load_skill_from_file(tmp_path):
    path = tmp_path / "inline.md"
    path.write_text(
        "---\nname: inline\ndescription: Inline skill.\n---\nDo the thing.",
        encoding="utf-8",
    )

    skill = load_skill(path)

    assert skill.name == "inline"
    assert skill.resources == []


def test_load_skill_defaults_name_to_directory(tmp_path):
    directory = tmp_path / "my-skill"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\ndescription: No name key.\n---\nBody.", encoding="utf-8"
    )

    skill = load_skill(directory)

    assert skill.name == "my-skill"


def test_load_skill_missing_description_raises(tmp_path):
    directory = tmp_path / "bad"
    directory.mkdir()
    (directory / "SKILL.md").write_text("---\nname: bad\n---\nBody.", encoding="utf-8")

    with pytest.raises(SkillError, match="description"):
        load_skill(directory)


def test_load_skill_empty_body_raises(tmp_path):
    directory = tmp_path / "empty"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: empty\ndescription: x\n---\n", encoding="utf-8"
    )

    with pytest.raises(SkillError, match="no instructions body"):
        load_skill(directory)


def test_load_skill_directory_without_skill_md_raises(tmp_path):
    directory = tmp_path / "nope"
    directory.mkdir()

    with pytest.raises(SkillError, match="missing a SKILL.md"):
        load_skill(directory)


def test_load_skill_nonexistent_path_raises(tmp_path):
    with pytest.raises(SkillError, match="does not exist"):
        load_skill(tmp_path / "ghost")


def test_discover_skills_local_then_shared_order(tmp_path):
    _write_skill(tmp_path / "skills" / "local", "local", "Local one.", "L")
    _write_skill(
        tmp_path / "shared" / "skills" / "shared", "shared", "Shared one.", "S"
    )
    spec = _make_spec(
        tmp_path, skills=["skills/local"], shared_skills=["skills/shared"]
    )

    skills = discover_skills(spec)

    assert [s.name for s in skills] == ["local", "shared"]


def test_discover_skills_rejects_duplicate_names(tmp_path):
    _write_skill(tmp_path / "a", "dup", "First.", "A")
    _write_skill(tmp_path / "shared" / "b", "dup", "Second.", "B")
    spec = _make_spec(tmp_path, skills=["a"], shared_skills=["b"])

    with pytest.raises(SkillError, match="Duplicate skill name"):
        discover_skills(spec)


def test_skills_instructions_empty_is_blank():
    assert skills_instructions([]) == ""


def test_skills_instructions_lists_names_and_descriptions():
    skills = [
        Skill(
            name="alpha", description="Do alpha.", instructions="x", source=Path(".")
        ),
        Skill(name="beta", description="Do beta.", instructions="y", source=Path(".")),
    ]
    text = skills_instructions(skills)

    assert "## Available skills" in text
    assert "load_skill" in text
    assert "- **alpha**: Do alpha." in text
    assert "- **beta**: Do beta." in text


def test_render_skill_includes_resources():
    skill = Skill(
        name="pdf",
        description="d",
        instructions="Step 1. Do it.",
        source=Path("SKILL.md"),
        resources=["helper.py"],
    )
    rendered = render_skill(skill)

    assert rendered.startswith("# Skill: pdf")
    assert "Step 1. Do it." in rendered
    assert "## Bundled resources" in rendered
    assert "- helper.py" in rendered


async def test_load_skill_tool_returns_body_and_handles_unknown():
    skills = [
        Skill(
            name="web-research",
            description="d",
            instructions="Search carefully.",
            source=Path("SKILL.md"),
        )
    ]
    tool = make_load_skill_tool(skills)
    func = tool.function

    found = await func("web-research")
    assert "Search carefully." in found

    missing = await func("nope")
    assert "Unknown skill 'nope'" in missing
    assert "web-research" in missing
