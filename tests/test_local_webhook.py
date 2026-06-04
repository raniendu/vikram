from urllib.parse import parse_qs

import pytest

from vikram import local_webhook
from vikram.local_webhook import (
    build_set_webhook_request,
    normalize_public_base_url,
    webhook_url,
)
from vikram.settings import VikramSettings
from vikram.telegram_config import TelegramBotConfig


def test_normalize_public_base_url_accepts_ngrok_base_url():
    base_url = normalize_public_base_url("https://abc123.ngrok-free.app/")

    assert base_url == "https://abc123.ngrok-free.app"
    assert webhook_url(base_url) == "https://abc123.ngrok-free.app/telegram/webhook"
    assert (
        webhook_url(base_url, bot_name="research")
        == "https://abc123.ngrok-free.app/telegram/research/webhook"
    )


def test_normalize_public_base_url_accepts_full_webhook_url():
    base_url = normalize_public_base_url(
        "https://abc123.ngrok-free.app/telegram/webhook"
    )

    assert base_url == "https://abc123.ngrok-free.app"


def test_normalize_public_base_url_accepts_full_named_webhook_url():
    base_url = normalize_public_base_url(
        "https://abc123.ngrok-free.app/telegram/research/webhook"
    )

    assert base_url == "https://abc123.ngrok-free.app"


@pytest.mark.parametrize(
    "url",
    [
        "http://abc123.ngrok-free.app",
        "https://localhost:8000",
        "https://127.0.0.1:8000",
        "https://abc123.ngrok-free.app/custom/path",
    ],
)
def test_normalize_public_base_url_rejects_bad_local_webhook_urls(url):
    with pytest.raises(ValueError):
        normalize_public_base_url(url)


def test_build_set_webhook_request_uses_form_body_without_printing_secret():
    settings = VikramSettings(
        _env_file=None,
        VIKRAM_TELEGRAM_BOT_TOKEN="bot-token",
        VIKRAM_TELEGRAM_WEBHOOK_SECRET="secret-token",
    )

    request = build_set_webhook_request(
        settings,
        "https://abc123.ngrok-free.app",
        drop_pending_updates=True,
    )

    body = parse_qs(request.data.decode("utf-8"))
    assert request.full_url == "https://api.telegram.org/botbot-token/setWebhook"
    assert body == {
        "url": ["https://abc123.ngrok-free.app/telegram/webhook"],
        "secret_token": ["secret-token"],
        "drop_pending_updates": ["true"],
    }


def test_build_set_webhook_request_uses_selected_bot():
    bot = TelegramBotConfig(
        name="research",
        default_agent="research",
        bot_token="bot-token",
        webhook_secret="secret-token",
        allowed_chat_ids="123",
        api_base_url="https://api.telegram.org",
    )

    request = build_set_webhook_request(
        bot,
        "https://abc123.ngrok-free.app",
        drop_pending_updates=False,
    )

    body = parse_qs(request.data.decode("utf-8"))
    assert request.full_url == "https://api.telegram.org/botbot-token/setWebhook"
    assert body == {
        "url": ["https://abc123.ngrok-free.app/telegram/research/webhook"],
        "secret_token": ["secret-token"],
        "drop_pending_updates": ["false"],
    }


def test_main_loads_named_bot_values_from_env_file(monkeypatch, tmp_path, capsys):
    env_file = write_research_webhook_env_file(tmp_path)
    for key in (
        "RESEARCH_TELEGRAM_BOT_TOKEN",
        "RESEARCH_TELEGRAM_WEBHOOK_SECRET",
        "RESEARCH_TELEGRAM_ALLOWED_CHAT_IDS",
        "RESEARCH_TELEGRAM_BOT_USERNAME",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(local_webhook, "check_health", lambda *args, **kwargs: None)

    selected_bots = []

    def fake_set_webhook(bot, *args, **kwargs):
        selected_bots.append(bot)
        return {"ok": True, "result": True}

    monkeypatch.setattr(local_webhook, "set_webhook", fake_set_webhook)

    exit_code = local_webhook.main(
        [
            "https://vikram.example.test",
            "--env-file",
            str(env_file),
            "--bot",
            "research",
        ]
    )

    assert exit_code == 0
    assert selected_bots[0].bot_token == "bot-token"
    assert selected_bots[0].webhook_secret == "secret-token"
    assert selected_bots[0].allowed_chat_id_set == {123}
    assert selected_bots[0].username == "ResearchBot"
    assert (
        "https://vikram.example.test/telegram/research/webhook"
        in capsys.readouterr().out
    )


def test_main_keeps_process_env_over_env_file_for_named_bot(monkeypatch, tmp_path):
    env_file = write_research_webhook_env_file(tmp_path)
    monkeypatch.setenv("RESEARCH_TELEGRAM_BOT_TOKEN", "shell-token")
    monkeypatch.setattr(local_webhook, "check_health", lambda *args, **kwargs: None)

    selected_bots = []

    def fake_set_webhook(bot, *args, **kwargs):
        selected_bots.append(bot)
        return {"ok": True, "result": True}

    monkeypatch.setattr(local_webhook, "set_webhook", fake_set_webhook)

    exit_code = local_webhook.main(
        [
            "https://vikram.example.test",
            "--env-file",
            str(env_file),
            "--bot",
            "research",
        ]
    )

    assert exit_code == 0
    assert selected_bots[0].bot_token == "shell-token"


def write_research_webhook_env_file(tmp_path):
    spec_root = tmp_path / "spec"
    spec_root.mkdir()
    (spec_root / "telegram.toml").write_text(
        """
default_bot = "research"

[[bots]]
name = "research"
default_agent = "vikram"
token_env = "RESEARCH_TELEGRAM_BOT_TOKEN"
webhook_secret_env = "RESEARCH_TELEGRAM_WEBHOOK_SECRET"
allowed_chat_ids_env = "RESEARCH_TELEGRAM_ALLOWED_CHAT_IDS"
username_env = "RESEARCH_TELEGRAM_BOT_USERNAME"
"""
    )
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "\n".join(
            [
                f"VIKRAM_SPEC_ROOT={spec_root}",
                "RESEARCH_TELEGRAM_BOT_TOKEN=bot-token",
                "RESEARCH_TELEGRAM_WEBHOOK_SECRET=secret-token",
                "RESEARCH_TELEGRAM_ALLOWED_CHAT_IDS=123",
                "RESEARCH_TELEGRAM_BOT_USERNAME=ResearchBot",
            ]
        )
    )
    return env_file
