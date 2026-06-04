import pytest
from pydantic_ai import Agent
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.models.openai import OpenAIChatModel

from vikram.agent import agent, build_agent
from vikram.settings import VikramSettings, build_model
from vikram.spec import AgentSpec

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


def clean_settings(monkeypatch, **overrides) -> VikramSettings:
    for env_var in VIKRAM_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)
    return VikramSettings(_env_file=None, **overrides)


def test_agent_is_configured_as_pydantic_ai_agent():
    assert isinstance(agent, Agent)
    assert agent.name == "Vikram"


def test_build_agent_uses_requested_settings(monkeypatch):
    local_agent = build_agent(
        settings=clean_settings(
            monkeypatch,
            VIKRAM_MODEL="qwen3",
            OLLAMA_BASE_URL="http://localhost:11434",
        )
    )

    assert isinstance(local_agent, Agent)
    assert local_agent.name == "Vikram"
    assert isinstance(local_agent.model, OllamaModel)
    assert local_agent.model.model_name == "qwen3"


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
        build_agent(spec=spec, settings=clean_settings(monkeypatch))

    message = str(exc_info.value)
    assert "Broken" in message
    assert "missing_tool" in message
    assert "vikram update" in message


def test_settings_default_to_local_ollama(monkeypatch):
    default_settings = clean_settings(monkeypatch)
    model = build_model(default_settings)

    assert isinstance(model, OllamaModel)
    assert model.model_name == "qwen3"
    assert default_settings.normalized_ollama_base_url == "http://localhost:11434/v1"
    assert model.provider.base_url.rstrip("/") == "http://localhost:11434/v1"
    assert default_settings.vikram_db_path.name == "vikram.sqlite3"
    assert default_settings.vikram_db_path.parent.name == ".vikram"


def test_build_model_uses_openai_compatible_when_provider_is_set(monkeypatch):
    settings = clean_settings(
        monkeypatch,
        VIKRAM_MODEL_PROVIDER="openai-compatible",
        VIKRAM_OPENAI_COMPAT_API_KEY="test-key",
        VIKRAM_OPENAI_COMPAT_BASE_URL="https://llm.example.test/v1",
        VIKRAM_MODEL="example-model",
    )
    model = build_model(settings)

    assert isinstance(model, OpenAIChatModel)
    assert model.model_name == "example-model"
    assert model.provider.base_url.rstrip("/") == "https://llm.example.test/v1"


def test_build_model_openai_compatible_requires_api_key(monkeypatch):
    settings = clean_settings(monkeypatch, VIKRAM_MODEL_PROVIDER="openai-compatible")
    with pytest.raises(RuntimeError, match="VIKRAM_OPENAI_COMPAT_API_KEY"):
        build_model(settings)
