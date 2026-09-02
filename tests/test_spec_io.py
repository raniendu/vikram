from __future__ import annotations

import tomllib

import pytest

from vikram.settings import VikramSettings
from vikram.spec import AgentSpec, AgentSpecDraft, load_spec
from vikram.spec_io import (
    SpecWriteError,
    read_agent_toml,
    render_agent_toml,
    write_agent_toml,
)

SHIPPED_AGENTS = ("vikram", "coder")


@pytest.fixture
def spec_root():
    return VikramSettings(_env_file=None).spec_root


def _draft_of(spec: AgentSpec) -> AgentSpecDraft:
    return AgentSpecDraft(**spec.model_dump(exclude={"agent_dir", "shared_dir"}))


def _comments(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip().startswith("#")]


@pytest.mark.parametrize("agent_id", SHIPPED_AGENTS)
def test_round_trip_preserves_every_comment(spec_root, agent_id):
    """The shipped specs are mostly teaching comments. Losing them is a bug."""
    original = (spec_root / agent_id / "agent.toml").read_text()
    spec = load_spec(agent_id, spec_root)

    rendered = render_agent_toml(_draft_of(spec), existing=original)

    assert _comments(rendered) == _comments(original)
    assert len(_comments(rendered)) > 20


@pytest.mark.parametrize("agent_id", SHIPPED_AGENTS)
def test_round_trip_reparses_to_an_identical_spec(spec_root, agent_id):
    original = (spec_root / agent_id / "agent.toml").read_text()
    spec = load_spec(agent_id, spec_root)

    rendered = render_agent_toml(_draft_of(spec), existing=original)
    rebuilt = AgentSpec(
        **tomllib.loads(rendered),
        agent_dir=spec.agent_dir,
        shared_dir=spec.shared_dir,
    )

    assert rebuilt == spec


def test_changing_one_key_leaves_the_rest_of_the_file_alone(spec_root):
    original = (spec_root / "vikram" / "agent.toml").read_text()
    spec = load_spec("vikram", spec_root)
    draft = _draft_of(spec).model_copy(update={"model": "some-other-model"})

    rendered = render_agent_toml(draft, existing=original)

    assert 'model = "some-other-model"' in rendered
    assert _comments(rendered) == _comments(original)
    assert tomllib.loads(rendered)["name"] == spec.name


def test_renders_arrays_of_tables_the_config_emitter_cannot():
    draft = AgentSpecDraft(
        name="Test",
        description="d",
        system_prompt="system_prompt.md",
        mcp_servers=[
            {"name": "fetch", "transport": "stdio", "command": "uvx"},
            {"name": "docs", "transport": "http", "url": "https://example.test/mcp"},
        ],
        hooks=[{"event": "PreToolUse", "transport": "command", "command": "true"}],
    )

    rendered = render_agent_toml(draft)
    parsed = tomllib.loads(rendered)

    assert rendered.count("[[mcp_servers]]") == 2
    assert rendered.count("[[hooks]]") == 1
    assert [s["name"] for s in parsed["mcp_servers"]] == ["fetch", "docs"]
    assert parsed["hooks"][0]["event"] == "PreToolUse"


def test_renders_model_settings_table():
    draft = AgentSpecDraft(
        name="Test",
        description="d",
        system_prompt="system_prompt.md",
        model_settings={"temperature": 0.2, "parallel_tool_calls": False},
    )

    parsed = tomllib.loads(render_agent_toml(draft))

    assert parsed["model_settings"] == {
        "temperature": 0.2,
        "parallel_tool_calls": False,
    }


def test_empty_collections_are_omitted_rather_than_written_as_empty():
    draft = AgentSpecDraft(name="T", description="d", system_prompt="p.md")

    rendered = render_agent_toml(draft)

    assert "skills" not in rendered
    assert "mcp_servers" not in rendered
    assert tomllib.loads(rendered)["name"] == "T"


def test_removing_a_server_drops_its_table(spec_root):
    draft = AgentSpecDraft(
        name="T",
        description="d",
        system_prompt="p.md",
        mcp_servers=[{"name": "fetch", "command": "uvx"}],
    )
    with_server = render_agent_toml(draft)

    without = render_agent_toml(
        draft.model_copy(update={"mcp_servers": []}), existing=with_server
    )

    assert "[[mcp_servers]]" not in without
    assert "mcp_servers" not in tomllib.loads(without)


def test_scalar_keys_are_written_in_a_stable_order():
    draft = AgentSpecDraft(
        name="T", description="d", system_prompt="p.md", model="m", tools=["grep"]
    )

    keys = [
        line.split(" = ")[0]
        for line in render_agent_toml(draft).splitlines()
        if " = " in line and not line.startswith("#")
    ]

    assert keys.index("name") < keys.index("description")
    assert keys.index("description") < keys.index("system_prompt")


def test_write_is_atomic_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "nested" / "agent.toml"

    write_agent_toml(path, 'name = "T"\n')

    assert path.read_text() == 'name = "T"\n'
    assert list(tmp_path.rglob("*.tmp")) == []


def test_read_returns_parsed_data_and_raw_text(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text('# a comment\nname = "T"\n')

    data, text = read_agent_toml(path)

    assert data == {"name": "T"}
    assert "# a comment" in text


def test_read_rejects_invalid_toml(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text("not [valid toml")

    with pytest.raises(SpecWriteError, match="Invalid TOML"):
        read_agent_toml(path)


def test_render_rejects_unparseable_existing_text():
    draft = AgentSpecDraft(name="T", description="d", system_prompt="p.md")

    with pytest.raises(SpecWriteError, match="Could not parse"):
        render_agent_toml(draft, existing="not [valid toml")
