from pathlib import Path

import pytest

from vikram.telegram_config import load_telegram_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def write_telegram_config(spec_root: Path, body: str) -> None:
    spec_root.mkdir(parents=True, exist_ok=True)
    (spec_root / "telegram.toml").write_text(body)


def test_load_telegram_config_resolves_bot_env_values(monkeypatch, tmp_path):
    spec_root = tmp_path / "spec"
    write_telegram_config(
        spec_root,
        """
default_bot = "vikram"

[[bots]]
name = "vikram"
default_agent = "vikram"
token_env = "VIKRAM_TELEGRAM_BOT_TOKEN"
webhook_secret_env = "VIKRAM_TELEGRAM_WEBHOOK_SECRET"
allowed_chat_ids_env = "VIKRAM_TELEGRAM_ALLOWED_CHAT_IDS"
api_base_url_env = "VIKRAM_TELEGRAM_API_BASE_URL"
username_env = "VIKRAM_TELEGRAM_BOT_USERNAME"
""",
    )
    monkeypatch.setenv("VIKRAM_TELEGRAM_BOT_TOKEN", "token-a")
    monkeypatch.setenv("VIKRAM_TELEGRAM_WEBHOOK_SECRET", "secret-a")
    monkeypatch.setenv("VIKRAM_TELEGRAM_ALLOWED_CHAT_IDS", "123,-100123")
    monkeypatch.setenv("VIKRAM_TELEGRAM_API_BASE_URL", "https://telegram.test")
    monkeypatch.setenv("VIKRAM_TELEGRAM_BOT_USERNAME", "@VikramBot")

    config = load_telegram_config(spec_root)
    bot = config.get_bot("vikram")

    assert config.default_bot_name == "vikram"
    assert bot.name == "vikram"
    assert bot.default_agent == "vikram"
    assert bot.bot_token == "token-a"
    assert bot.webhook_secret == "secret-a"
    assert bot.allowed_chat_id_set == {123, -100123}
    assert bot.api_base_url == "https://telegram.test"
    assert bot.username == "VikramBot"
    assert bot.webhook_path == "/telegram/vikram/webhook"


def test_load_telegram_config_rejects_duplicate_bot_names(monkeypatch, tmp_path):
    spec_root = tmp_path / "spec"
    write_telegram_config(
        spec_root,
        """
default_bot = "vikram"

[[bots]]
name = "vikram"
default_agent = "vikram"
token_env = "BOT_TOKEN"
webhook_secret_env = "BOT_SECRET"
allowed_chat_ids_env = "BOT_CHAT_IDS"

[[bots]]
name = "vikram"
default_agent = "other"
token_env = "OTHER_BOT_TOKEN"
webhook_secret_env = "OTHER_BOT_SECRET"
allowed_chat_ids_env = "OTHER_BOT_CHAT_IDS"
""",
    )
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("BOT_SECRET", "secret")
    monkeypatch.setenv("BOT_CHAT_IDS", "123")
    monkeypatch.setenv("OTHER_BOT_TOKEN", "token")
    monkeypatch.setenv("OTHER_BOT_SECRET", "secret")
    monkeypatch.setenv("OTHER_BOT_CHAT_IDS", "123")

    with pytest.raises(ValueError, match="Duplicate Telegram bot name"):
        load_telegram_config(spec_root)


def test_load_telegram_config_falls_back_to_legacy_single_bot(monkeypatch, tmp_path):
    spec_root = tmp_path / "spec"
    spec_root.mkdir()
    monkeypatch.setenv("VIKRAM_TELEGRAM_BOT_TOKEN", "legacy-token")
    monkeypatch.setenv("VIKRAM_TELEGRAM_WEBHOOK_SECRET", "legacy-secret")
    monkeypatch.setenv("VIKRAM_TELEGRAM_ALLOWED_CHAT_IDS", "123")

    config = load_telegram_config(spec_root)
    bot = config.get_default_bot()

    assert config.default_bot_name == "telegram"
    assert bot.name == "telegram"
    assert bot.default_agent == "vikram"
    assert bot.bot_token == "legacy-token"
    assert bot.webhook_secret == "legacy-secret"
    assert bot.allowed_chat_id_set == {123}
    assert bot.username is None
    assert bot.webhook_path == "/telegram/webhook"


def test_load_telegram_config_leaves_empty_username_unset(monkeypatch, tmp_path):
    spec_root = tmp_path / "spec"
    write_telegram_config(
        spec_root,
        """
default_bot = "vikram"

[[bots]]
name = "vikram"
default_agent = "vikram"
token_env = "BOT_TOKEN"
webhook_secret_env = "BOT_SECRET"
allowed_chat_ids_env = "BOT_CHAT_IDS"
username_env = "BOT_USERNAME"
""",
    )
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("BOT_SECRET", "secret")
    monkeypatch.setenv("BOT_CHAT_IDS", "123")
    monkeypatch.setenv("BOT_USERNAME", "")

    config = load_telegram_config(spec_root)

    assert config.get_bot("vikram").username is None


def test_checked_in_telegram_config_includes_vikram_bot(monkeypatch):
    spec_root = REPO_ROOT / "spec"
    monkeypatch.setenv("VIKRAM_TELEGRAM_BOT_TOKEN", "vikram-token")
    monkeypatch.setenv("VIKRAM_TELEGRAM_WEBHOOK_SECRET", "vikram-secret")
    monkeypatch.setenv("VIKRAM_TELEGRAM_ALLOWED_CHAT_IDS", "123,-100123")
    monkeypatch.setenv("VIKRAM_TELEGRAM_BOT_USERNAME", "VikramBot")

    config = load_telegram_config(spec_root)
    bot = config.get_bot("vikram")

    assert config.default_bot_name == "vikram"
    assert bot.name == "vikram"
    assert bot.default_agent == "vikram"
    assert bot.bot_token == "vikram-token"
    assert bot.webhook_secret == "vikram-secret"
    assert bot.allowed_chat_id_set == {123, -100123}
    assert bot.username == "VikramBot"
    assert bot.interface == "telegram:vikram"
    assert bot.webhook_path == "/telegram/vikram/webhook"
