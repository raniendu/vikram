"""Provider adapter registry: the single source of truth for model providers.

Each :class:`Provider` describes where a provider's credentials and endpoint
live on ``VikramSettings``, how the ``vikram configure`` wizard should prompt
for it, and how to build the underlying Pydantic AI model. ``settings.py``,
``config.py`` and ``spec.py`` all import from here; this module must not
import from any ``vikram`` module so it stays cycle-free, and Pydantic AI
imports happen inside the builder functions so importing the registry stays
cheap.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

ModelProvider = Literal[
    "ollama",
    "ollama-cloud",
    "anthropic",
    "gemini",
    "openai",
    "digitalocean",
    "openai-compatible",
]


def ensure_v1_suffix(base_url: str) -> str:
    """Normalize a host or base URL to the OpenAI-compatible ``/v1`` form."""
    value = base_url.strip().rstrip("/")
    if value.endswith("/v1"):
        return value
    return f"{value}/v1"


@dataclass(frozen=True)
class ModelRequest:
    """Resolved inputs handed to a provider's build function."""

    model: str
    params: dict[str, Any]
    api_key: str | None = None
    base_url: str | None = None


@dataclass(frozen=True)
class Provider:
    id: str
    display_name: str
    build: Callable[[ModelRequest], Any]
    needs_api_key: bool = False
    api_key_field: str | None = None
    api_key_env: str | None = None
    base_url_field: str | None = None
    default_base_url: str | None = None
    normalize_base_url: Callable[[str], str] | None = None
    suggested_model: str | None = None
    prompt_base_url: bool = False
    base_url_hint: str | None = None


def _build_ollama(request: ModelRequest) -> Any:
    from pydantic_ai.models.ollama import OllamaModel
    from pydantic_ai.providers.ollama import OllamaProvider

    return OllamaModel(
        request.model,
        provider=OllamaProvider(base_url=request.base_url),
    )


def _build_ollama_cloud(request: ModelRequest) -> Any:
    from pydantic_ai.models.ollama import OllamaModel
    from pydantic_ai.providers.ollama import OllamaProvider

    return OllamaModel(
        request.model,
        provider=OllamaProvider(base_url=request.base_url, api_key=request.api_key),
    )


def _build_anthropic(request: ModelRequest) -> Any:
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider

    return AnthropicModel(
        request.model,
        provider=AnthropicProvider(api_key=request.api_key),
    )


def _build_gemini(request: ModelRequest) -> Any:
    from pydantic_ai.models.google import GoogleModel
    from pydantic_ai.providers.google import GoogleProvider

    return GoogleModel(
        request.model,
        provider=GoogleProvider(api_key=request.api_key),
    )


def _build_openai_compatible(request: ModelRequest) -> Any:
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    return OpenAIChatModel(
        request.model,
        provider=OpenAIProvider(
            base_url=request.base_url,
            api_key=request.api_key,
        ),
    )


PROVIDERS: dict[str, Provider] = {
    entry.id: entry
    for entry in (
        Provider(
            id="ollama",
            display_name="Ollama (local)",
            build=_build_ollama,
            base_url_field="ollama_base_url",
            default_base_url="http://localhost:11434/v1",
            normalize_base_url=ensure_v1_suffix,
            suggested_model="llama3.2",
            prompt_base_url=True,
            base_url_hint="blank uses http://localhost:11434",
        ),
        Provider(
            id="ollama-cloud",
            display_name="Ollama Cloud",
            build=_build_ollama_cloud,
            needs_api_key=True,
            api_key_field="ollama_api_key",
            api_key_env="OLLAMA_API_KEY",
            base_url_field="ollama_cloud_base_url",
            default_base_url="https://ollama.com",
            normalize_base_url=ensure_v1_suffix,
            suggested_model="gpt-oss:120b",
        ),
        Provider(
            id="anthropic",
            display_name="Anthropic Claude",
            build=_build_anthropic,
            needs_api_key=True,
            api_key_field="anthropic_api_key",
            api_key_env="ANTHROPIC_API_KEY",
            suggested_model="claude-sonnet-5",
        ),
        Provider(
            id="gemini",
            display_name="Google Gemini",
            build=_build_gemini,
            needs_api_key=True,
            api_key_field="gemini_api_key",
            api_key_env="GEMINI_API_KEY",
            suggested_model="gemini-2.5-flash",
        ),
        Provider(
            id="openai",
            display_name="OpenAI",
            build=_build_openai_compatible,
            needs_api_key=True,
            api_key_field="openai_api_key",
            api_key_env="OPENAI_API_KEY",
            base_url_field="openai_base_url",
            default_base_url="https://api.openai.com/v1",
            suggested_model="gpt-5-mini",
        ),
        Provider(
            id="digitalocean",
            display_name="DigitalOcean inference",
            build=_build_openai_compatible,
            needs_api_key=True,
            api_key_field="digitalocean_api_key",
            api_key_env="DIGITALOCEAN_ACCESS_TOKEN",
            base_url_field="digitalocean_base_url",
            default_base_url="https://inference.do-ai.run/v1",
            suggested_model="llama3.3-70b-instruct",
        ),
        Provider(
            id="openai-compatible",
            display_name="OpenAI-compatible (custom endpoint)",
            build=_build_openai_compatible,
            needs_api_key=True,
            api_key_field="openai_compat_api_key",
            api_key_env="VIKRAM_OPENAI_COMPAT_API_KEY",
            base_url_field="openai_compat_base_url",
            default_base_url="https://api.openai.com/v1",
            prompt_base_url=True,
            base_url_hint="e.g. Sarvam AI: https://api.sarvam.ai/v1",
        ),
    )
}

PROVIDER_IDS: tuple[str, ...] = tuple(PROVIDERS)
