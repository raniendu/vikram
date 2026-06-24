from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import PydanticBaseSettingsSource

from vikram.config import load_config

ModelProvider = Literal["ollama", "openai-compatible"]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VikramModel:
    """A Strands model plus stable metadata used by tests and adapters."""

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
    ollama_base_url: str = Field(
        default="http://localhost:11434/v1",
        validation_alias="OLLAMA_BASE_URL",
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
    telegram_bot_username: str | None = Field(
        default=None, validation_alias="VIKRAM_TELEGRAM_BOT_USERNAME"
    )
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


SUPPORTED_MODEL_SETTINGS = {
    "temperature",
    "top_p",
    "max_tokens",
    "stop_sequences",
    "frequency_penalty",
    "presence_penalty",
}


def map_model_settings(
    values: dict[str, Any] | None, *, agent_name: str
) -> dict[str, Any]:
    """Best-effort map of Vikram spec settings to Strands provider params."""
    mapped: dict[str, Any] = {}
    for key, value in (values or {}).items():
        if key in SUPPORTED_MODEL_SETTINGS:
            mapped[key] = value
        else:
            logger.warning(
                "unsupported_model_setting_ignored: %s",
                key,
                extra={"agent": agent_name, "setting": key},
            )
    return mapped


def build_model(
    settings: VikramSettings | None = None,
    *,
    model_settings: dict[str, Any] | None = None,
    agent_name: str = "agent",
) -> VikramModel:
    settings = settings or VikramSettings()
    if not settings.model_provider:
        raise RuntimeError(
            "Vikram model provider is not configured. Run `vikram configure` "
            "or set VIKRAM_MODEL_PROVIDER."
        )
    if not settings.model:
        raise RuntimeError(
            "Vikram model is not configured. Run `vikram configure` or set "
            "VIKRAM_MODEL."
        )
    params = map_model_settings(model_settings, agent_name=agent_name)
    if settings.model_provider == "ollama":
        from strands.models.ollama import OllamaModel

        raw = OllamaModel(
            host=settings.normalized_ollama_host,
            model_id=settings.model,
            **params,
        )
        return VikramModel(
            raw=raw,
            config={
                "provider": "ollama",
                "model": settings.model,
                "base_url": settings.normalized_ollama_host,
                "params": params,
            },
        )
    if settings.model_provider == "openai-compatible":
        if not settings.openai_compat_api_key:
            raise RuntimeError(
                "VIKRAM_OPENAI_COMPAT_API_KEY is not set. Run `vikram "
                "configure`, add it to .env, or set it in the runtime "
                "environment to use the openai-compatible model provider."
            )
        from strands.models.openai import OpenAIModel

        raw = OpenAIModel(
            client_args={
                "base_url": settings.openai_compat_base_url,
                "api_key": settings.openai_compat_api_key,
            },
            model_id=settings.model,
            params=params,
        )
        return VikramModel(
            raw=raw,
            config={
                "provider": "openai-compatible",
                "model": settings.model,
                "base_url": settings.openai_compat_base_url,
                "params": params,
            },
        )
    raise RuntimeError(f"Unknown VIKRAM_MODEL_PROVIDER: {settings.model_provider!r}")
