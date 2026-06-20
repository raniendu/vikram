from types import SimpleNamespace

import pytest
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServer
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.models.openai import OpenAIChatModel

from vikram.agent import build_agent
from vikram.delegation import make_delegate_to_agent_tool
from vikram.mcp import MCPServerSpec
from vikram.settings import VikramSettings, build_model
from vikram.spec import AgentSpec, load_spec

VIKRAM_ENV_VARS = (
    "VIKRAM_MODEL",
    "OLLAMA_BASE_URL",
    "VIKRAM_SPEC_ROOT",
    "VIKRAM_AGENT",
    "VIKRAM_MODEL_PROVIDER",
    "VIKRAM_OPENAI_COMPAT_API_KEY",
    "VIKRAM_OPENAI_COMPAT_BASE_URL",
    "OPENAI_API_KEY",
)


def clean_settings(monkeypatch, tmp_path, **overrides) -> VikramSettings:
    for env_var in VIKRAM_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config"))
    return VikramSettings(_env_file=None, **overrides)


def test_build_agent_uses_requested_settings(monkeypatch, tmp_path):
    local_agent = build_agent(
        settings=clean_settings(
            monkeypatch,
            tmp_path,
            VIKRAM_MODEL_PROVIDER="ollama",
            VIKRAM_MODEL="test-model",
            OLLAMA_BASE_URL="http://localhost:11434",
        )
    )

    assert isinstance(local_agent, Agent)
    assert local_agent.name == "Vikram"
    assert isinstance(local_agent.model, OllamaModel)
    assert local_agent.model.model_name == "test-model"


def test_coder_spec_defaults_to_qwen_mlx(monkeypatch, tmp_path):
    settings = clean_settings(monkeypatch, tmp_path)
    spec = load_spec("coder", settings.spec_root)

    agent = build_agent(spec=spec, settings=settings)

    assert isinstance(agent.model, OllamaModel)
    assert agent.model.model_name == "qwen3.6:35b-mlx"


def test_environment_overrides_coder_spec_model(monkeypatch, tmp_path):
    settings = clean_settings(
        monkeypatch,
        tmp_path,
        VIKRAM_MODEL_PROVIDER="ollama",
        VIKRAM_MODEL="env-model",
    )
    spec = load_spec("coder", settings.spec_root)

    agent = build_agent(spec=spec, settings=settings)

    assert isinstance(agent.model, OllamaModel)
    assert agent.model.model_name == "env-model"


def test_build_agent_reports_unknown_tools(monkeypatch, tmp_path):
    (tmp_path / "system_prompt.md").write_text("PROMPT", encoding="utf-8")
    spec = AgentSpec(
        name="Broken",
        description="Spec with a missing tool",
        system_prompt=tmp_path / "system_prompt.md",
        tools=["missing_tool"],
        agent_dir=tmp_path,
        shared_dir=tmp_path / "shared",
    )

    with pytest.raises(RuntimeError) as exc_info:
        build_agent(spec=spec, settings=clean_settings(monkeypatch, tmp_path))

    message = str(exc_info.value)
    assert "Broken" in message
    assert "missing_tool" in message
    assert "vikram update" in message


def test_build_model_requires_provider_and_model(monkeypatch, tmp_path):
    settings = clean_settings(monkeypatch, tmp_path)

    with pytest.raises(RuntimeError) as exc_info:
        build_model(settings)

    message = str(exc_info.value)
    assert "vikram configure" in message
    assert "VIKRAM_MODEL_PROVIDER" in message


def test_build_model_requires_model_name(monkeypatch, tmp_path):
    settings = clean_settings(
        monkeypatch,
        tmp_path,
        VIKRAM_MODEL_PROVIDER="ollama",
    )

    with pytest.raises(RuntimeError) as exc_info:
        build_model(settings)

    message = str(exc_info.value)
    assert "vikram configure" in message
    assert "VIKRAM_MODEL" in message


def test_settings_load_model_from_local_config(monkeypatch, tmp_path):
    config_dir = tmp_path / "vikram"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        "\n".join(
            [
                'model_provider = "ollama"',
                'model = "llama3.2"',
                'ollama_base_url = "http://localhost:11434"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    for env_var in VIKRAM_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    settings = VikramSettings(_env_file=None)
    model = build_model(settings)

    assert isinstance(model, OllamaModel)
    assert model.model_name == "llama3.2"
    assert settings.normalized_ollama_base_url == "http://localhost:11434/v1"
    assert model.provider.base_url.rstrip("/") == "http://localhost:11434/v1"
    assert settings.vikram_db_path.name == "vikram.sqlite3"
    assert settings.vikram_db_path.parent.name == ".vikram"


def test_environment_overrides_local_model_config(monkeypatch, tmp_path):
    config_dir = tmp_path / "vikram"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        "\n".join(
            [
                'model_provider = "ollama"',
                'model = "from-config"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    for env_var in VIKRAM_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("VIKRAM_MODEL", "from-env")

    settings = VikramSettings(_env_file=None)

    assert settings.model_provider == "ollama"
    assert settings.model == "from-env"


def test_build_model_uses_openai_compatible_when_provider_is_set(monkeypatch, tmp_path):
    settings = clean_settings(
        monkeypatch,
        tmp_path,
        VIKRAM_MODEL_PROVIDER="openai-compatible",
        VIKRAM_OPENAI_COMPAT_API_KEY="test-key",
        VIKRAM_OPENAI_COMPAT_BASE_URL="https://llm.example.test/v1",
        VIKRAM_MODEL="example-model",
    )
    model = build_model(settings)

    assert isinstance(model, OpenAIChatModel)
    assert model.model_name == "example-model"
    assert model.provider.base_url.rstrip("/") == "https://llm.example.test/v1"


def test_build_model_openai_compatible_requires_api_key(monkeypatch, tmp_path):
    settings = clean_settings(
        monkeypatch,
        tmp_path,
        VIKRAM_MODEL_PROVIDER="openai-compatible",
        VIKRAM_MODEL="example-model",
    )
    with pytest.raises(RuntimeError, match="VIKRAM_OPENAI_COMPAT_API_KEY"):
        build_model(settings)


def _local_model_settings(monkeypatch, tmp_path) -> VikramSettings:
    return clean_settings(
        monkeypatch,
        tmp_path,
        VIKRAM_MODEL_PROVIDER="ollama",
        VIKRAM_MODEL="test-model",
        OLLAMA_BASE_URL="http://localhost:11434",
    )


def test_build_agent_registers_load_skill_when_spec_has_skills(monkeypatch, tmp_path):
    settings = _local_model_settings(monkeypatch, tmp_path)
    spec = load_spec("vikram", settings.spec_root)

    agent = build_agent(spec=spec, settings=settings)

    # The real vikram spec ships the web-research skill, so build_agent attaches
    # the load_skill tool alongside the spec's own tools.
    assert "load_skill" in agent._function_toolset.tools
    assert "web_search" in agent._function_toolset.tools


def test_vikram_agent_registers_subagent_delegation_tool(monkeypatch, tmp_path):
    settings = _local_model_settings(monkeypatch, tmp_path)
    spec = load_spec("vikram", settings.spec_root)

    agent = build_agent(spec=spec, settings=settings, surface="cli")

    assert "delegate_to_agent" in agent._function_toolset.tools
    assert agent._function_toolset.tools["delegate_to_agent"].requires_approval is True
    instruction_text = "\n\n".join(str(item) for item in agent._instructions)
    assert "## Available subagents" in instruction_text
    assert "`coder`" in instruction_text
    assert "CLI-only coding agent" in instruction_text


def test_coder_agent_does_not_register_subagent_delegation_tool(monkeypatch, tmp_path):
    settings = _local_model_settings(monkeypatch, tmp_path)
    spec = load_spec("coder", settings.spec_root)

    agent = build_agent(spec=spec, settings=settings, surface="cli")

    assert "delegate_to_agent" not in agent._function_toolset.tools


async def test_delegate_to_agent_runs_target_agent(monkeypatch, tmp_path):
    settings = _local_model_settings(monkeypatch, tmp_path)
    calls = []

    class FakeAgent:
        async def run(self, prompt, *, conversation_id, capabilities=None):
            calls.append((prompt, conversation_id, capabilities))
            return SimpleNamespace(output="implemented")

    def fake_build_agent(**kwargs):
        calls.append(kwargs)
        return FakeAgent()

    monkeypatch.setattr("vikram.agent.build_agent", fake_build_agent)
    tool = make_delegate_to_agent_tool(
        settings=settings,
        orchestrator_name="vikram",
        surface="cli",
        requires_approval=False,
    )

    result = await tool.function("coder", "Implement the requested code change.")

    assert result == "Subagent Coder completed.\n\nimplemented"
    assert calls[0]["spec"].name == "Coder"
    assert calls[0]["settings"] is settings
    assert calls[0]["surface"] == "cli"
    assert calls[0]["enable_delegation"] is False
    assert calls[1][0] == "Implement the requested code change."
    assert calls[1][1] == "delegate:vikram:coder"
    assert calls[1][2], "delegated subagent runs should receive capabilities"


async def test_delegate_to_agent_rejects_path_like_agent_names(monkeypatch, tmp_path):
    spec_root = tmp_path / "spec"
    _write_minimal_agent_spec(
        spec_root,
        "coder",
        display_name="Coder",
        description="CLI-only coding agent for local repository work.",
    )
    _write_minimal_agent_spec(
        tmp_path,
        "evil",
        display_name="Evil",
        description="Outside spec root.",
    )
    settings = _local_model_settings(monkeypatch, tmp_path).model_copy(
        update={"spec_root": spec_root}
    )
    build_calls = []

    def fake_build_agent(**kwargs):
        build_calls.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr("vikram.agent.build_agent", fake_build_agent)
    tool = make_delegate_to_agent_tool(
        settings=settings,
        orchestrator_name="vikram",
        surface="cli",
        requires_approval=False,
    )

    for agent_name in (
        "../evil",
        "coder/../evil",
        "..",
        str((tmp_path / "evil").resolve()),
    ):
        result = await tool.function(agent_name, "Do not load this spec.")
        assert "Unknown agent" in result
        assert "coder" in result

    assert build_calls == []


async def test_delegate_to_agent_fails_when_subagent_requests_approval(
    monkeypatch, tmp_path
):
    settings = _local_model_settings(monkeypatch, tmp_path)

    class FakeRequests:
        approvals = [
            SimpleNamespace(tool_call_id="c1", tool_name="write_file"),
        ]
        calls = []

        def build_results(self, *, approvals, calls):
            return SimpleNamespace(approvals=approvals, calls=calls)

    class FakeAgent:
        async def run(self, prompt, *, conversation_id, capabilities=None):
            await capabilities[0].handler(None, FakeRequests())
            return SimpleNamespace(output="approval was allowed")

    monkeypatch.setattr("vikram.agent.build_agent", lambda **kwargs: FakeAgent())
    tool = make_delegate_to_agent_tool(
        settings=settings,
        orchestrator_name="vikram",
        surface="cli",
        requires_approval=False,
    )

    result = await tool.function("coder", "Edit a file.")

    assert "requested approval-gated tool calls" in result
    assert "write_file" in result
    assert "Run that agent directly" in result


async def test_delegate_to_agent_rejects_cli_only_agent_on_http_surface(
    monkeypatch, tmp_path
):
    settings = _local_model_settings(monkeypatch, tmp_path)
    tool = make_delegate_to_agent_tool(
        settings=settings,
        orchestrator_name="vikram",
        surface="http",
        requires_approval=False,
    )

    result = await tool.function("coder", "Implement a code change.")

    assert "Cannot delegate to 'coder'" in result
    assert "CLI-only" in result


async def test_delegate_to_agent_refuses_self_delegation(monkeypatch, tmp_path):
    settings = _local_model_settings(monkeypatch, tmp_path)
    tool = make_delegate_to_agent_tool(
        settings=settings,
        orchestrator_name="vikram",
        surface="cli",
        requires_approval=False,
    )

    result = await tool.function("vikram", "Do this yourself.")

    assert "cannot delegate to itself" in result


def test_build_agent_without_skills_has_no_load_skill(monkeypatch, tmp_path):
    settings = _local_model_settings(monkeypatch, tmp_path)
    (tmp_path / "system_prompt.md").write_text("PROMPT", encoding="utf-8")
    spec = AgentSpec(
        name="Plain",
        description="No skills",
        system_prompt=tmp_path / "system_prompt.md",
        agent_dir=tmp_path,
        # Reuse the real shared dir so the command policy file resolves.
        shared_dir=settings.spec_root / "shared",
    )

    agent = build_agent(spec=spec, settings=settings)

    assert "load_skill" not in agent._function_toolset.tools


def _write_minimal_agent_spec(
    spec_root,
    name: str,
    *,
    display_name: str,
    description: str,
):
    agent_dir = spec_root / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.toml").write_text(
        "\n".join(
            [
                f'name = "{display_name}"',
                f'description = "{description}"',
                'system_prompt = "system_prompt.md"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (agent_dir / "system_prompt.md").write_text("PROMPT", encoding="utf-8")


def test_build_agent_attaches_mcp_servers_as_toolsets(monkeypatch, tmp_path):
    settings = _local_model_settings(monkeypatch, tmp_path)
    monkeypatch.setenv("DEMO_MCP_TOKEN", "tok")
    (tmp_path / "system_prompt.md").write_text("PROMPT", encoding="utf-8")
    spec = AgentSpec(
        name="MCPAgent",
        description="demo",
        system_prompt=tmp_path / "system_prompt.md",
        agent_dir=tmp_path,
        # Reuse the real shared dir so the command policy file resolves.
        shared_dir=settings.spec_root / "shared",
        mcp_servers=[
            MCPServerSpec(
                name="github",
                command="npx",
                args=["-y", "srv"],
                env={"TOKEN": "${DEMO_MCP_TOKEN}"},
            )
        ],
    )

    agent = build_agent(spec=spec, settings=settings)

    servers = [t for t in agent.toolsets if isinstance(t, MCPServer)]
    assert [s.id for s in servers] == ["github"]
    assert servers[0].env == {"TOKEN": "tok"}
