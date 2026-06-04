from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_TELEGRAM_API_BASE_URL = "https://api.telegram.org"
LEGACY_BOT_NAME = "telegram"
TELEGRAM_CONFIG_FILE = "telegram.toml"
BOT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
BOT_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


@dataclass(frozen=True)
class TelegramBotConfig:
    name: str
    default_agent: str
    bot_token: str
    webhook_secret: str
    allowed_chat_ids: str
    username: str | None = None
    api_base_url: str = DEFAULT_TELEGRAM_API_BASE_URL
    legacy: bool = False

    def __post_init__(self) -> None:
        if not BOT_NAME_RE.fullmatch(self.name):
            raise ValueError(
                f"Invalid Telegram bot name: {self.name!r}. "
                "Use only letters, numbers, underscore, or hyphen."
            )
        username = normalize_bot_username(self.username)
        if username is not None and not BOT_USERNAME_RE.fullmatch(username):
            raise ValueError(
                f"Invalid Telegram bot username: {self.username!r}. "
                "Use only letters, numbers, or underscore."
            )
        object.__setattr__(self, "username", username)

    @property
    def interface(self) -> str:
        if self.legacy:
            return "telegram"
        return f"telegram:{self.name}"

    @property
    def webhook_path(self) -> str:
        if self.legacy:
            return "/telegram/webhook"
        return f"/telegram/{self.name}/webhook"

    @property
    def allowed_chat_id_set(self) -> set[int]:
        chat_ids: set[int] = set()
        for raw in self.allowed_chat_ids.split(","):
            value = raw.strip()
            if value:
                chat_ids.add(int(value))
        return chat_ids


@dataclass(frozen=True)
class TelegramConfig:
    default_bot_name: str
    bots: dict[str, TelegramBotConfig]

    def get_bot(self, name: str) -> TelegramBotConfig:
        try:
            return self.bots[name]
        except KeyError as exc:
            raise KeyError(f"Unknown Telegram bot: {name}") from exc

    def get_default_bot(self) -> TelegramBotConfig:
        return self.get_bot(self.default_bot_name)


def load_telegram_config(
    spec_root: Path,
    *,
    default_agent: str = "vikram",
    env: Mapping[str, str | None] | None = None,
) -> TelegramConfig:
    if env is None:
        env = os.environ
    config_path = spec_root / TELEGRAM_CONFIG_FILE
    if not config_path.exists():
        return legacy_telegram_config(default_agent=default_agent, env=env)

    raw = tomllib.loads(config_path.read_text())
    default_bot_name = str(raw.get("default_bot", "")).strip()
    raw_bots = raw.get("bots", [])
    if not default_bot_name:
        raise ValueError("Telegram config must define default_bot.")
    if not isinstance(raw_bots, list) or not raw_bots:
        raise ValueError("Telegram config must define at least one [[bots]] entry.")

    bots: dict[str, TelegramBotConfig] = {}
    for item in raw_bots:
        if not isinstance(item, dict):
            raise ValueError("Each Telegram bot config must be a table.")
        bot = _bot_from_table(item, env)
        if bot.name in bots:
            raise ValueError(f"Duplicate Telegram bot name: {bot.name}")
        bots[bot.name] = bot

    if default_bot_name not in bots:
        raise ValueError(
            f"default_bot references unknown Telegram bot: {default_bot_name}"
        )
    return TelegramConfig(default_bot_name=default_bot_name, bots=bots)


def legacy_telegram_config(
    *, default_agent: str = "vikram", env: Mapping[str, str | None] | None = None
) -> TelegramConfig:
    if env is None:
        env = os.environ
    bot = TelegramBotConfig(
        name=LEGACY_BOT_NAME,
        default_agent=default_agent,
        bot_token=_env_value(env, "VIKRAM_TELEGRAM_BOT_TOKEN"),
        webhook_secret=_env_value(env, "VIKRAM_TELEGRAM_WEBHOOK_SECRET"),
        allowed_chat_ids=_env_value(env, "VIKRAM_TELEGRAM_ALLOWED_CHAT_IDS"),
        username=normalize_bot_username(
            _env_value(env, "VIKRAM_TELEGRAM_BOT_USERNAME")
        ),
        api_base_url=_env_value(
            env, "VIKRAM_TELEGRAM_API_BASE_URL", DEFAULT_TELEGRAM_API_BASE_URL
        ),
        legacy=True,
    )
    return TelegramConfig(default_bot_name=bot.name, bots={bot.name: bot})


def _bot_from_table(
    item: dict[str, Any], env: Mapping[str, str | None]
) -> TelegramBotConfig:
    name = _required_str(item, "name")
    return TelegramBotConfig(
        name=name,
        default_agent=_required_str(item, "default_agent"),
        bot_token=_env_value(env, _required_str(item, "token_env")),
        webhook_secret=_env_value(env, _required_str(item, "webhook_secret_env")),
        allowed_chat_ids=_env_value(env, _required_str(item, "allowed_chat_ids_env")),
        username=_optional_env(item, "username_env", env),
        api_base_url=_api_base_url(item, env),
    )


def _api_base_url(item: dict[str, Any], env: Mapping[str, str | None]) -> str:
    raw_env = item.get("api_base_url_env")
    if raw_env is None:
        return DEFAULT_TELEGRAM_API_BASE_URL
    env_name = str(raw_env).strip()
    if not env_name:
        return DEFAULT_TELEGRAM_API_BASE_URL
    return _env_value(env, env_name, DEFAULT_TELEGRAM_API_BASE_URL)


def _optional_env(
    item: dict[str, Any], key: str, env: Mapping[str, str | None]
) -> str | None:
    raw_env = item.get(key)
    if raw_env is None:
        return None
    env_name = str(raw_env).strip()
    if not env_name:
        return None
    return normalize_bot_username(_env_value(env, env_name))


def _env_value(env: Mapping[str, str | None], key: str, default: str = "") -> str:
    value = env.get(key)
    if value is None:
        return default
    return value


def _required_str(item: dict[str, Any], key: str) -> str:
    value = str(item.get(key, "")).strip()
    if not value:
        raise ValueError(f"Telegram bot config must define {key}.")
    return value


def normalize_bot_username(value: str | None) -> str | None:
    if value is None:
        return None
    username = value.strip()
    if username.startswith("@"):
        username = username[1:]
    return username or None
