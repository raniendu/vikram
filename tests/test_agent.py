import pytest
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServer
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.models.openai import OpenAIChatModel

from vikram.agent import build_agent
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
