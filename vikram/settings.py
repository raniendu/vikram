from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_ai.models import Model
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.ollama import OllamaProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_settings import BaseSettings, SettingsConfigDict

ModelProvider = Literal["ollama", "openai-compatible"]


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


class VikramSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        populate_by_name=True,
    )

    model_provider: ModelProvider = Field(
        default="ollama", validation_alias="VIKRAM_MODEL_PROVIDER"
    )
    model: str = Field(
        default="qwen3",
        validation_alias="VIKRAM_MODEL",
    )
    ollama_base_url: str = Field(
        default="http://localhost:11434/v1",
        validation_alias="OLLAMA_BASE_URL",
    )
    openai_compat_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("VIKRAM_OPENAI_COMPAT_API_KEY", "OPENAI_API_KEY"),
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

    @property
    def normalized_ollama_base_url(self) -> str:
        base_url = self.ollama_base_url.strip().rstrip("/")
        if base_url.endswith("/v1"):
            return base_url
        return f"{base_url}/v1"

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


def build_model(settings: VikramSettings | None = None) -> Model:
    settings = settings or VikramSettings()
    if settings.model_provider == "ollama":
        return OllamaModel(
            settings.model,
            provider=OllamaProvider(base_url=settings.normalized_ollama_base_url),
        )
    if settings.model_provider == "openai-compatible":
        if not settings.openai_compat_api_key:
            raise RuntimeError(
                "VIKRAM_OPENAI_COMPAT_API_KEY is not set. Add it to .env or "
                "the runtime environment to use the openai-compatible model "
                "provider."
            )
        return OpenAIChatModel(
            settings.model,
            provider=OpenAIProvider(
                base_url=settings.openai_compat_base_url,
                api_key=settings.openai_compat_api_key,
            ),
        )
    raise RuntimeError(f"Unknown VIKRAM_MODEL_PROVIDER: {settings.model_provider!r}")
