from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import PydanticBaseSettingsSource

from vikram.config import load_config
from vikram.logging import get_logger
from vikram.providers import PROVIDER_IDS, PROVIDERS, ModelProvider, ModelRequest

logger = get_logger(__name__)


@dataclass(frozen=True)
class VikramModel:
    """A Pydantic AI model plus stable metadata used by adapters and UX."""

    raw: Any
    config: dict[str, Any]


def _resolve_spec_root(package_relative: Path) -> Path:
    """Locate spec/ for both dev (in-checkout) and installed (`uv tool`) layouts.

    Dev: ``<package>/../spec`` exists as a sibling of the package.
    Installed: package lives in site-packages; spec ships separately, so we
    fall back to the source checkout recorded by ``install.sh`` at
    ``~/.config/vikram/install.toml``.
    """
    if package_relative.is_dir():
        return package_relative
    try:
        from vikram.update import load_metadata
    except Exception:
        return package_relative
    source_dir = load_metadata().get("source_dir")
    if source_dir:
        root = Path(str(source_dir))
        for candidate in (root / "spec", root / "apps" / "vikram" / "spec"):
            if candidate.is_dir():
                return candidate
    return package_relative


def _default_spec_root() -> Path:
    return _resolve_spec_root(Path(__file__).resolve().parent.parent / "spec")


class VikramConfigSettingsSource(PydanticBaseSettingsSource):
    def get_field_value(self, field, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return load_config()


class VikramSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        populate_by_name=True,
    )

    model_provider: ModelProvider | None = Field(
        default=None, validation_alias="VIKRAM_MODEL_PROVIDER"
    )
    model: str | None = Field(
        default=None,
        validation_alias="VIKRAM_MODEL",
    )
    provider_models: dict[str, str] = Field(
        default_factory=dict,
        validation_alias="VIKRAM_PROVIDER_MODELS",
    )
    # Populated only by the config.toml source (no env alias on purpose):
    # the file's default_provider must rank below agent spec pins, while an
    # explicit VIKRAM_MODEL_PROVIDER (model_provider above) ranks above them.
    config_default_provider: str | None = None
    agent_overrides: dict[str, dict[str, str]] = Field(default_factory=dict)
    ollama_base_url: str = Field(
        default="http://localhost:11434/v1",
        validation_alias="OLLAMA_BASE_URL",
    )
    ollama_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("VIKRAM_OLLAMA_API_KEY", "OLLAMA_API_KEY"),
    )
    ollama_cloud_base_url: str = Field(
        default="https://ollama.com",
        validation_alias="VIKRAM_OLLAMA_CLOUD_BASE_URL",
    )
    anthropic_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("VIKRAM_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
    )
    gemini_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "VIKRAM_GEMINI_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
        ),
    )
    openai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("VIKRAM_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )
    openai_base_url: str = Field(
        default="https://api.openai.com/v1",
        validation_alias="VIKRAM_OPENAI_BASE_URL",
    )
    digitalocean_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "VIKRAM_DIGITALOCEAN_API_KEY",
            "DIGITALOCEAN_ACCESS_TOKEN",
        ),
    )
    digitalocean_base_url: str = Field(
        default="https://inference.do-ai.run/v1",
        validation_alias="VIKRAM_DIGITALOCEAN_BASE_URL",
    )
    openai_compat_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "VIKRAM_OPENAI_COMPAT_API_KEY",
            "OPENAI_API_KEY",
            "DIGITALOCEAN_ACCESS_TOKEN",
            "SARVAM_API_KEY",
        ),
    )
    openai_compat_base_url: str = Field(
        default="https://api.openai.com/v1",
        validation_alias="VIKRAM_OPENAI_COMPAT_BASE_URL",
    )
    spec_root: Path = Field(
        default_factory=_default_spec_root,
        validation_alias="VIKRAM_SPEC_ROOT",
    )
    default_agent: str = Field(default="vikram", validation_alias="VIKRAM_AGENT")
    parallel_api_key: str | None = Field(
        default=None, validation_alias="PARALLEL_API_KEY"
    )
    vikram_db_path: Path = Field(
        default=Path(__file__).resolve().parent.parent / ".vikram" / "vikram.sqlite3",
        validation_alias="VIKRAM_DB_PATH",
    )
    dbos_system_database_url: str | None = Field(
        default=None, validation_alias="DBOS_SYSTEM_DATABASE_URL"
    )
    public_base_url: str | None = Field(
        default=None, validation_alias="VIKRAM_PUBLIC_BASE_URL"
    )
    telegram_bot_token: str | None = Field(
        default=None, validation_alias="VIKRAM_TELEGRAM_BOT_TOKEN"
    )
    telegram_webhook_secret: str | None = Field(
        default=None, validation_alias="VIKRAM_TELEGRAM_WEBHOOK_SECRET"
    )
    telegram_allowed_chat_ids: str = Field(
        default="", validation_alias="VIKRAM_TELEGRAM_ALLOWED_CHAT_IDS"
    )
    telegram_api_base_url: str = Field(
        default="https://api.telegram.org",
        validation_alias="VIKRAM_TELEGRAM_API_BASE_URL",
    )
    # NOTE: VIKRAM_TELEGRAM_BOT_USERNAME is deliberately absent here. Bot
    # usernames are per-bot and resolved by vikram.telegram_config straight
    # from the environment via spec/telegram.toml's ``username_env``; a field
    # here would look authoritative while being read by nothing.
    log_level: str = Field(default="INFO", validation_alias="VIKRAM_LOG_LEVEL")
    environment: str = Field(default="local", validation_alias="ENVIRONMENT")
    observability_enabled: bool = Field(
        default=False, validation_alias="VIKRAM_OBSERVABILITY_ENABLED"
    )
    observability_service_name: str = Field(
        default="vikram", validation_alias="VIKRAM_OBSERVABILITY_SERVICE_NAME"
    )
    observability_otlp_endpoint: str | None = Field(
        default=None, validation_alias="VIKRAM_OTLP_ENDPOINT"
    )
    observability_capture_message_content: bool = Field(
        default=False,
        validation_alias="VIKRAM_OBSERVABILITY_CAPTURE_MESSAGE_CONTENT",
    )
    observability_disable_metrics: bool = Field(
        default=False, validation_alias="VIKRAM_OBSERVABILITY_DISABLE_METRICS"
    )
    observability_disabled_instrumentors: str = Field(
        default="mistral",
        validation_alias="VIKRAM_OBSERVABILITY_DISABLED_INSTRUMENTORS",
    )
    context_window_tokens: int = Field(
        default=256_000,
        validation_alias="VIKRAM_CONTEXT_WINDOW_TOKENS",
    )
    context_warning_ratio: float = Field(
        default=0.85,
        validation_alias="VIKRAM_CONTEXT_WARNING_RATIO",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            VikramConfigSettingsSource(settings_cls),
            file_secret_settings,
        )

    @property
    def normalized_ollama_base_url(self) -> str:
        base_url = self.ollama_base_url.strip().rstrip("/")
        if base_url.endswith("/v1"):
            return base_url
        return f"{base_url}/v1"

    @property
    def normalized_ollama_host(self) -> str:
        base_url = self.ollama_base_url.strip().rstrip("/")
        if base_url.endswith("/v1"):
            return base_url[: -len("/v1")]
        return base_url

    @property
    def telegram_allowed_chat_id_set(self) -> set[int]:
        chat_ids: set[int] = set()
        for raw in self.telegram_allowed_chat_ids.split(","):
            value = raw.strip()
            if value:
                chat_ids.add(int(value))
        return chat_ids

    @property
    def effective_dbos_system_database_url(self) -> str:
        if self.dbos_system_database_url:
            return self.dbos_system_database_url
        return f"sqlite:///{self.vikram_db_path.parent / 'dbos.sqlite3'}"

    @property
    def observability_disabled_instrumentor_list(self) -> list[str] | None:
        values = [
            value.strip()
            for value in self.observability_disabled_instrumentors.split(",")
            if value.strip()
        ]
        return values or None


def map_model_settings(
    values: dict[str, Any] | None, *, agent_name: str
) -> dict[str, Any]:
    """Return spec model settings for Pydantic AI's provider-aware validation."""
    del agent_name
    return dict(values or {})


def resolve_model_selection(settings: Any) -> tuple[str | None, str | None]:
    """Resolve the effective (provider, model) pair for a settings object.

    ``settings.model`` (env/CLI/spec) wins; otherwise fall back to the
    provider's own default model from ``provider_models`` (config.toml).
    Accepts any settings-like object so lightweight test doubles work.
    """
    provider = getattr(settings, "model_provider", None) or getattr(
        settings, "config_default_provider", None
    )
    model = getattr(settings, "model", None)
    if not model and provider:
        provider_models = getattr(settings, "provider_models", None) or {}
        model = provider_models.get(provider) or None
    return provider, model


def resolve_agent_model_selection(
    settings: Any,
    *,
    agent_id: str | None = None,
    spec_provider: str | None = None,
    spec_model: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve (provider, model) for one agent, spec pins included.

    Precedence: explicit env/CLI > saved per-agent choice (``[agents.<id>]``,
    written by ``/model``) > spec pin > config ``default_provider``, with the
    provider's own ``[providers.<id>]`` model as the final fallback. A saved
    or pinned model only applies when its provider matches the resolved
    provider.
    """
    override = (getattr(settings, "agent_overrides", None) or {}).get(
        agent_id or ""
    ) or {}
    provider = (
        getattr(settings, "model_provider", None)
        or override.get("provider")
        or spec_provider
        or getattr(settings, "config_default_provider", None)
    )
    model = getattr(settings, "model", None)
    override_provider = override.get("provider")
    if (
        not model
        and override.get("model")
        and (not override_provider or override_provider == provider)
    ):
        model = override["model"]
    if not model and spec_model and provider == spec_provider:
        model = spec_model
    if not model and provider:
        model = (getattr(settings, "provider_models", None) or {}).get(provider)
    return provider, model


def build_model(
    settings: VikramSettings | None = None,
    *,
    model_settings: dict[str, Any] | None = None,
    agent_name: str = "agent",
) -> VikramModel:
    settings = settings or VikramSettings()
    provider_id, model = resolve_model_selection(settings)
    if not provider_id:
        raise RuntimeError(
            "Vikram model provider is not configured. Run `vikram configure` "
            "or set VIKRAM_MODEL_PROVIDER."
        )
    provider = PROVIDERS.get(provider_id)
    if provider is None:
        raise RuntimeError(
            f"Unknown VIKRAM_MODEL_PROVIDER: {provider_id!r}. "
            f"Valid providers: {', '.join(PROVIDER_IDS)}."
        )
    if not model:
        raise RuntimeError(
            "Vikram model is not configured. Run `vikram configure` or set "
            "VIKRAM_MODEL."
        )

    params = map_model_settings(model_settings, agent_name=agent_name)
    base_url: str | None = None
    if provider.base_url_field:
        base_url = (
            getattr(settings, provider.base_url_field) or provider.default_base_url
        )
        if base_url and provider.normalize_base_url:
            base_url = provider.normalize_base_url(base_url)

    api_key: str | None = None
    if provider.api_key_field:
        api_key = getattr(settings, provider.api_key_field)
    if provider.needs_api_key and not api_key:
        raise RuntimeError(
            f"{provider.api_key_env} is not set. Run `vikram configure`, add "
            f"it to .env, or set it in the runtime environment to use the "
            f"{provider_id} model provider."
        )

    raw = provider.build(
        ModelRequest(model=model, params=params, api_key=api_key, base_url=base_url)
    )
    # base_url is an endpoint, never a credential; api_key stays out entirely.
    logger.info(
        "model_built",
        agent=agent_name,
        model_provider=provider_id,
        model=model,
        base_url=base_url,
        model_settings=sorted(params),
    )
    return VikramModel(
        raw=raw,
        config={
            "provider": provider_id,
            "model": model,
            "base_url": base_url,
            "params": params,
        },
    )
