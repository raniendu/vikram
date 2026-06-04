from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from pydantic_settings import DotEnvSettingsSource

from vikram.settings import VikramSettings
from vikram.telegram_config import TelegramBotConfig, load_telegram_config

WEBHOOK_PATH = "/telegram/webhook"
NAMED_WEBHOOK_RE = re.compile(r"^/telegram/[A-Za-z0-9_-]+/webhook$")
SUPPORTED_TELEGRAM_PORTS = {80, 88, 443, 8443}


def normalize_public_base_url(raw_url: str) -> str:
    value = raw_url.strip().rstrip("/")
    if value.endswith(WEBHOOK_PATH):
        value = value[: -len(WEBHOOK_PATH)].rstrip("/")
    parts = urlsplit(value)
    if NAMED_WEBHOOK_RE.fullmatch(parts.path):
        value = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        parts = urlsplit(value)
    host = parts.hostname or ""
    if parts.scheme != "https":
        raise ValueError("Telegram webhooks require an https:// public URL.")
    if host in {"localhost", "127.0.0.1", "0.0.0.0"}:
        raise ValueError("Telegram cannot call localhost; use an HTTPS tunnel URL.")
    if parts.path not in {"", "/"}:
        raise ValueError(
            "Pass the ngrok base URL only, or an exact Telegram webhook URL."
        )
    if parts.port is not None and parts.port not in SUPPORTED_TELEGRAM_PORTS:
        raise ValueError("Telegram supports webhook ports 443, 80, 88, and 8443.")
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def webhook_url(public_base_url: str, *, bot_name: str | None = None) -> str:
    if bot_name:
        return (
            f"{normalize_public_base_url(public_base_url)}/telegram/{bot_name}/webhook"
        )
    return f"{normalize_public_base_url(public_base_url)}{WEBHOOK_PATH}"


def build_set_webhook_request(
    target: VikramSettings | TelegramBotConfig,
    public_base_url: str,
    *,
    drop_pending_updates: bool,
) -> Request:
    if isinstance(target, TelegramBotConfig):
        bot_token = target.bot_token
        webhook_secret = target.webhook_secret
        api_base_url = target.api_base_url
        target_webhook_url = (
            f"{normalize_public_base_url(public_base_url)}{target.webhook_path}"
        )
        bot_label = target.name
    else:
        bot_token = target.telegram_bot_token
        webhook_secret = target.telegram_webhook_secret
        api_base_url = target.telegram_api_base_url
        target_webhook_url = webhook_url(public_base_url)
        bot_label = "legacy"
    if not bot_token:
        raise RuntimeError(f"Telegram bot token is required for {bot_label}.")
    if not webhook_secret:
        raise RuntimeError(f"Telegram webhook secret is required for {bot_label}.")
    body = urlencode(
        {
            "url": target_webhook_url,
            "secret_token": webhook_secret,
            "drop_pending_updates": "true" if drop_pending_updates else "false",
        }
    ).encode("utf-8")
    return Request(
        f"{api_base_url.rstrip('/')}" f"/bot{bot_token}/setWebhook",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )


def check_health(public_base_url: str, *, timeout: float) -> None:
    request = Request(f"{normalize_public_base_url(public_base_url)}/healthz")
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload != {"status": "ok"}:
        raise RuntimeError(f"Unexpected /healthz response: {payload!r}")


def set_webhook(
    target: VikramSettings | TelegramBotConfig,
    public_base_url: str,
    *,
    drop_pending_updates: bool,
    timeout: float,
) -> dict[str, Any]:
    request = build_set_webhook_request(
        target,
        public_base_url,
        drop_pending_updates=drop_pending_updates,
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def env_for_config(env_file: str) -> dict[str, str]:
    values: dict[str, str] = {}
    env_path = Path(env_file)
    if env_path.exists():
        source = DotEnvSettingsSource(
            VikramSettings,
            env_file=env_path,
            env_file_encoding="utf-8",
            case_sensitive=True,
            env_ignore_empty=True,
        )
        values.update(
            {key: value for key, value in source.env_vars.items() if value is not None}
        )
    values.update(os.environ)
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Point a configured Vikram Telegram webhook at a public HTTPS URL."
    )
    parser.add_argument(
        "public_base_url",
        help="ngrok HTTPS base URL, or the full Telegram webhook URL.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Env file containing Telegram secrets. Defaults to .env.",
    )
    parser.add_argument(
        "--no-drop-pending",
        action="store_false",
        dest="drop_pending_updates",
        help="Do not ask Telegram to discard pending updates when switching.",
    )
    parser.add_argument(
        "--bot",
        help="Configured Telegram bot name to register. Defaults to default_bot.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Register webhooks for all configured Telegram bots.",
    )
    parser.add_argument(
        "--skip-health-check",
        action="store_true",
        help="Set the webhook without checking <public-base-url>/healthz first.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Network timeout in seconds. Defaults to 10.",
    )
    args = parser.parse_args(argv)

    try:
        base_url = normalize_public_base_url(args.public_base_url)
        settings = VikramSettings(_env_file=args.env_file)
        if not args.skip_health_check:
            check_health(base_url, timeout=args.timeout)
        config = load_telegram_config(
            settings.spec_root,
            default_agent=settings.default_agent,
            env=env_for_config(args.env_file),
        )
        if args.all:
            selected_bots = list(config.bots.values())
        else:
            selected_bots = [config.get_bot(args.bot or config.default_bot_name)]
        results = [
            {
                "bot": bot.name,
                "webhook_url": f"{base_url}{bot.webhook_path}",
                "telegram_result": set_webhook(
                    bot,
                    base_url,
                    drop_pending_updates=args.drop_pending_updates,
                    timeout=args.timeout,
                ),
            }
            for bot in selected_bots
        ]
    except (HTTPError, URLError, OSError, RuntimeError, ValueError) as exc:
        print(f"Failed to set Vikram Telegram webhook: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            results[0] if len(results) == 1 else {"webhooks": results},
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
