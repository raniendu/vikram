from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
import telegramify_markdown

from vikram.gateway import InboundMessage, MessageEnqueuer, ThreadStore
from vikram.logging import chat_hash, get_logger
from vikram.settings import VikramSettings
from vikram.spec import AgentSurfaceError, ensure_surface_allowed, load_spec
from vikram.telegram_config import (
    LEGACY_BOT_NAME,
    TelegramBotConfig,
    normalize_bot_username,
)

MAX_TELEGRAM_MESSAGE = 4096
TELEGRAM_PARSE_MODE = "MarkdownV2"
TELEGRAM_TYPING_ACTION = "typing"
GROUP_CHAT_TYPES = {"group", "supergroup"}
GROUP_ADMIN_STATUSES = {"administrator", "creator"}
logger = get_logger(__name__)


@dataclass(frozen=True)
class TelegramWebhookResult:
    status: str
    workflow_id: str | None = None


@dataclass(frozen=True)
class TelegramTextMessage:
    update_id: int
    message_id: int
    chat_id: int
    chat_type: str
    text: str
    from_id: int | None
    from_username: str | None
    from_first_name: str | None
    from_last_name: str | None
    reply_to_message_id: int | None
    reply_to_from_username: str | None


@dataclass(frozen=True)
class TelegramCommand:
    name: str
    target: str | None
    arg: str


SendText = Callable[[int, str, int | None], Awaitable[None]]
SendChatAction = Callable[[int, str], Awaitable[None]]
GetChatMember = Callable[[int, int], Awaitable[dict[str, Any]]]


class TelegramAdapter:
    def __init__(
        self,
        *,
        settings: VikramSettings,
        bot: TelegramBotConfig | None = None,
        store: ThreadStore,
        enqueue_message: MessageEnqueuer | None,
        send_text: SendText | None = None,
        send_chat_action: SendChatAction | None = None,
        get_chat_member: GetChatMember | None = None,
    ):
        self.settings = settings
        self.bot = bot or TelegramBotConfig(
            name=LEGACY_BOT_NAME,
            default_agent=settings.default_agent,
            bot_token=settings.telegram_bot_token or "",
            webhook_secret=settings.telegram_webhook_secret or "",
            allowed_chat_ids=settings.telegram_allowed_chat_ids,
            api_base_url=settings.telegram_api_base_url,
            legacy=True,
        )
        self.store = store
        self.enqueue_message = enqueue_message
        self._send_text = send_text
        self._send_chat_action = send_chat_action
        self._get_chat_member = get_chat_member

    async def handle_update(self, update: dict[str, Any]) -> TelegramWebhookResult:
        update_id = update.get("update_id")
        log = logger.bind(update_id=update_id)
        log.info("telegram_update_received")
        message = parse_text_message(update)
        if message is None:
            result = await self._handle_non_text_update(update)
            log.info("telegram_update_processed", status=result.status)
            return result
        log = log.bind(
            chat_hash=chat_hash(message.chat_id),
            chat_type=message.chat_type,
            text_length=len(message.text),
            has_from_id=message.from_id is not None,
            has_reply_to_message=message.reply_to_message_id is not None,
        )
        if not self.store.claim_telegram_update(self.bot.name, message.update_id):
            log.info("telegram_update_duplicate")
            return TelegramWebhookResult(status="duplicate")
        if message.chat_id not in self.bot.allowed_chat_id_set:
            log.warning("telegram_chat_rejected")
            await self.send_message(message.chat_id, "This bot is private.")
            return TelegramWebhookResult(status="rejected")
        if message.text.startswith("/"):
            command = parse_bot_command(message.text)
            if command is None:
                log.info("telegram_update_ignored", reason="invalid_command")
                return TelegramWebhookResult(status="ignored")
            if not self._command_addresses_this_bot(command.target):
                log.info("telegram_update_ignored", reason="command_for_other_bot")
                return TelegramWebhookResult(status="ignored")
            result = await self._handle_command(message, command)
            log.info("telegram_command_processed", status=result.status)
            return result
        prompt = message.text
        trigger = "private"
        if is_group_chat(message.chat_type):
            route = self._group_message_route(message)
            if route is None:
                log.info("telegram_update_ignored", reason="group_not_directed")
                return TelegramWebhookResult(status="ignored")
            prompt, trigger = route
            if not prompt:
                log.info("telegram_update_ignored", reason="empty_group_prompt")
                return TelegramWebhookResult(status="ignored")
            prompt = format_group_prompt(message, prompt)
        if self.enqueue_message is None:
            log.error("telegram_enqueue_missing")
            raise RuntimeError("Telegram enqueue_message callback is not configured")
        await self._send_working_indicator(message)
        enqueued = await self.enqueue_message(
            InboundMessage(
                interface=self.bot.interface,
                external_thread_id=thread_id_for_message(message),
                prompt=prompt,
                agent_name=None,
                default_agent=self.bot.default_agent,
                metadata=message_metadata(message, self.bot.name, trigger),
            )
        )
        log.info(
            "telegram_message_enqueued",
            workflow_id=enqueued.workflow_id,
            status=enqueued.status,
        )
        return TelegramWebhookResult(
            status=enqueued.status,
            workflow_id=enqueued.workflow_id,
        )

    async def send_message(
        self, chat_id: int, text: str, reply_to_message_id: int | None = None
    ) -> None:
        chunks = format_for_telegram(text)
        log = logger.bind(
            chat_hash=chat_hash(chat_id),
            chunk_count=len(chunks),
            output_length=len(text),
            has_reply_to_message_id=reply_to_message_id is not None,
        )
        log.info("telegram_send_started")
        if self._send_text is not None:
            for index, chunk in enumerate(chunks, start=1):
                await self._send_text(chat_id, chunk, reply_to_message_id)
                log.info(
                    "telegram_send_chunk_succeeded",
                    chunk_index=index,
                    transport="injected",
                )
            log.info("telegram_send_succeeded")
            return
        if not self.bot.bot_token:
            log.error("telegram_send_unconfigured")
            raise RuntimeError(
                f"Telegram bot token is not configured for {self.bot.name}"
            )
        async with httpx.AsyncClient(base_url=self.bot.api_base_url) as client:
            for index, chunk in enumerate(chunks, start=1):
                try:
                    payload: dict[str, Any] = {
                        "chat_id": chat_id,
                        "text": chunk,
                        "parse_mode": TELEGRAM_PARSE_MODE,
                    }
                    if reply_to_message_id is not None:
                        payload["reply_to_message_id"] = reply_to_message_id
                    response = await client.post(
                        f"/bot{self.bot.bot_token}/sendMessage",
                        json=payload,
                    )
                    response.raise_for_status()
                    log.info(
                        "telegram_send_chunk_succeeded",
                        chunk_index=index,
                        transport="telegram_api",
                        status_code=response.status_code,
                    )
                except httpx.HTTPError:
                    log.exception(
                        "telegram_send_chunk_failed",
                        chunk_index=index,
                        transport="telegram_api",
                    )
                    raise
        log.info("telegram_send_succeeded")

    async def send_chat_action(
        self, chat_id: int, action: str = TELEGRAM_TYPING_ACTION
    ) -> None:
        log = logger.bind(chat_hash=chat_hash(chat_id), action=action)
        log.info("telegram_chat_action_started")
        if self._send_chat_action is not None:
            await self._send_chat_action(chat_id, action)
            log.info("telegram_chat_action_succeeded", transport="injected")
            return
        if not self.bot.bot_token:
            log.error("telegram_chat_action_unconfigured")
            raise RuntimeError(
                f"Telegram bot token is not configured for {self.bot.name}"
            )
        async with httpx.AsyncClient(base_url=self.bot.api_base_url) as client:
            try:
                response = await client.post(
                    f"/bot{self.bot.bot_token}/sendChatAction",
                    json={"chat_id": chat_id, "action": action},
                )
                response.raise_for_status()
            except httpx.HTTPError:
                raise RuntimeError("Telegram sendChatAction request failed") from None
        log.info("telegram_chat_action_succeeded", transport="telegram_api")

    async def _send_working_indicator(self, message: TelegramTextMessage) -> None:
        try:
            await self.send_chat_action(message.chat_id)
        except Exception:
            logger.exception(
                "telegram_chat_action_failed",
                update_id=message.update_id,
                chat_hash=chat_hash(message.chat_id),
                action=TELEGRAM_TYPING_ACTION,
            )

    async def _handle_command(
        self, message: TelegramTextMessage, command: TelegramCommand
    ) -> TelegramWebhookResult:
        logger.info(
            "telegram_command_received",
            update_id=message.update_id,
            chat_hash=chat_hash(message.chat_id),
            command=command.name,
        )
        if command.name in {"/start", "/help"}:
            await self.send_message(
                message.chat_id,
                "Send a text message to chat with the agent. Use /reset to clear history or /agent <name> to switch agents.",
                reply_to_message_id=reply_to_message_id_for_response(message),
            )
            return TelegramWebhookResult(status="handled")
        if command.name == "/reset":
            if not await self._ensure_group_admin(message, command.name):
                return TelegramWebhookResult(status="handled")
            self.store.reset_history(self.bot.interface, thread_id_for_message(message))
            await self.send_message(
                message.chat_id,
                "Conversation reset.",
                reply_to_message_id=reply_to_message_id_for_response(message),
            )
            return TelegramWebhookResult(status="handled")
        if command.name == "/agent":
            if not await self._ensure_group_admin(message, command.name):
                return TelegramWebhookResult(status="handled")
            agent_name = command.arg.strip()
            if not agent_name:
                await self.send_message(
                    message.chat_id,
                    "Usage: /agent <name>",
                    reply_to_message_id=reply_to_message_id_for_response(message),
                )
                return TelegramWebhookResult(status="handled")
            try:
                spec = load_spec(agent_name, self.settings.spec_root)
                ensure_surface_allowed(spec, "telegram")
            except FileNotFoundError:
                await self.send_message(
                    message.chat_id,
                    f"Unknown agent: {agent_name}",
                    reply_to_message_id=reply_to_message_id_for_response(message),
                )
                return TelegramWebhookResult(status="handled")
            except AgentSurfaceError:
                await self.send_message(
                    message.chat_id,
                    f"Agent {agent_name} is only available in the local CLI.",
                    reply_to_message_id=reply_to_message_id_for_response(message),
                )
                return TelegramWebhookResult(status="handled")
            self.store.set_agent(
                self.bot.interface, thread_id_for_message(message), agent_name
            )
            await self.send_message(
                message.chat_id,
                f"Agent set to {agent_name}.",
                reply_to_message_id=reply_to_message_id_for_response(message),
            )
            return TelegramWebhookResult(status="handled")
        await self.send_message(
            message.chat_id,
            f"Unknown command: {command.name}",
            reply_to_message_id=reply_to_message_id_for_response(message),
        )
        return TelegramWebhookResult(status="handled")

    async def _ensure_group_admin(
        self, message: TelegramTextMessage, command: str
    ) -> bool:
        if not is_group_chat(message.chat_type):
            return True
        if message.from_id is None:
            logger.warning(
                "telegram_group_admin_check_failed",
                update_id=message.update_id,
                chat_hash=chat_hash(message.chat_id),
                reason="missing_from_id",
            )
            await self.send_message(
                message.chat_id,
                "I couldn't verify Telegram admin status. Try again later.",
                reply_to_message_id=reply_to_message_id_for_response(message),
            )
            return False
        try:
            member = await self._fetch_chat_member(message.chat_id, message.from_id)
        except Exception:
            logger.exception(
                "telegram_group_admin_check_failed",
                update_id=message.update_id,
                chat_hash=chat_hash(message.chat_id),
                reason="telegram_api_error",
            )
            await self.send_message(
                message.chat_id,
                "I couldn't verify Telegram admin status. Try again later.",
                reply_to_message_id=reply_to_message_id_for_response(message),
            )
            return False
        status = str(member.get("status", "")).lower()
        if status in GROUP_ADMIN_STATUSES:
            return True
        await self.send_message(
            message.chat_id,
            f"Only Telegram chat admins can use {command} in groups.",
            reply_to_message_id=reply_to_message_id_for_response(message),
        )
        return False

    async def _fetch_chat_member(self, chat_id: int, user_id: int) -> dict[str, Any]:
        if self._get_chat_member is not None:
            return await self._get_chat_member(chat_id, user_id)
        if not self.bot.bot_token:
            raise RuntimeError(
                f"Telegram bot token is not configured for {self.bot.name}"
            )
        async with httpx.AsyncClient(base_url=self.bot.api_base_url) as client:
            try:
                response = await client.post(
                    f"/bot{self.bot.bot_token}/getChatMember",
                    json={"chat_id": chat_id, "user_id": user_id},
                )
                response.raise_for_status()
            except httpx.HTTPError:
                raise RuntimeError("Telegram getChatMember request failed") from None
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError("Telegram getChatMember returned an error")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Telegram getChatMember result is not an object")
        return result

    def _command_addresses_this_bot(self, target: str | None) -> bool:
        if target is None:
            return True
        bot_username = normalize_bot_username(self.bot.username)
        return bot_username is not None and target.lower() == bot_username.lower()

    def _group_message_route(
        self, message: TelegramTextMessage
    ) -> tuple[str, str] | None:
        mentioned, prompt = strip_bot_mentions(message.text, self.bot.username)
        if mentioned:
            return prompt, "mention"
        bot_username = normalize_bot_username(self.bot.username)
        reply_username = normalize_bot_username(message.reply_to_from_username)
        if (
            bot_username is not None
            and reply_username is not None
            and reply_username.lower() == bot_username.lower()
        ):
            return message.text.strip(), "reply"
        return None

    async def _handle_non_text_update(
        self, update: dict[str, Any]
    ) -> TelegramWebhookResult:
        chat_id = extract_chat_id(update)
        update_id = update.get("update_id")
        log = logger.bind(
            update_id=update_id,
            chat_hash=chat_hash(chat_id) if chat_id is not None else None,
        )
        if chat_id is None or update_id is None:
            log.info("telegram_update_ignored", reason="missing_chat_or_update_id")
            return TelegramWebhookResult(status="ignored")
        if not self.store.claim_telegram_update(self.bot.name, int(update_id)):
            log.info("telegram_update_duplicate")
            return TelegramWebhookResult(status="duplicate")
        if chat_id not in self.bot.allowed_chat_id_set:
            log.warning("telegram_chat_rejected")
            await self.send_message(chat_id, "This bot is private.")
            return TelegramWebhookResult(status="rejected")
        chat_type = extract_chat_type(update)
        if chat_type is not None and is_group_chat(chat_type):
            log.info("telegram_update_ignored", reason="group_non_text")
            return TelegramWebhookResult(status="ignored")
        log.info("telegram_non_text_rejected")
        await self.send_message(chat_id, "Text messages only for now.")
        return TelegramWebhookResult(status="rejected")


def parse_text_message(update: dict[str, Any]) -> TelegramTextMessage | None:
    raw_message = update.get("message")
    if not isinstance(raw_message, dict):
        return None
    text = raw_message.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    chat = raw_message.get("chat")
    if not isinstance(chat, dict) or "id" not in chat:
        return None
    raw_from = raw_message.get("from")
    from_id = raw_from.get("id") if isinstance(raw_from, dict) else None
    reply_to_message = raw_message.get("reply_to_message")
    reply_to_message_id = (
        reply_to_message.get("message_id")
        if isinstance(reply_to_message, dict)
        else None
    )
    reply_to_from = (
        reply_to_message.get("from") if isinstance(reply_to_message, dict) else None
    )
    reply_to_from_username = (
        reply_to_from.get("username") if isinstance(reply_to_from, dict) else None
    )
    return TelegramTextMessage(
        update_id=int(update["update_id"]),
        message_id=int(raw_message["message_id"]),
        chat_id=int(chat["id"]),
        chat_type=str(chat.get("type", "unknown")),
        text=text.strip(),
        from_id=int(from_id) if from_id is not None else None,
        from_username=_optional_str(raw_from, "username"),
        from_first_name=_optional_str(raw_from, "first_name"),
        from_last_name=_optional_str(raw_from, "last_name"),
        reply_to_message_id=(
            int(reply_to_message_id) if reply_to_message_id is not None else None
        ),
        reply_to_from_username=(
            str(reply_to_from_username).strip()
            if reply_to_from_username is not None
            else None
        ),
    )


def extract_chat_id(update: dict[str, Any]) -> int | None:
    raw_message = update.get("message")
    if not isinstance(raw_message, dict):
        return None
    chat = raw_message.get("chat")
    if not isinstance(chat, dict) or "id" not in chat:
        return None
    return int(chat["id"])


def extract_chat_type(update: dict[str, Any]) -> str | None:
    raw_message = update.get("message")
    if not isinstance(raw_message, dict):
        return None
    chat = raw_message.get("chat")
    if not isinstance(chat, dict):
        return None
    raw_type = chat.get("type")
    return str(raw_type) if raw_type is not None else None


def parse_bot_command(text: str) -> TelegramCommand | None:
    command_token, _, arg = text.strip().partition(" ")
    if not command_token.startswith("/"):
        return None
    command_body = command_token[1:]
    if not command_body:
        return None
    name, separator, target = command_body.partition("@")
    if not name:
        return None
    return TelegramCommand(
        name=f"/{name.lower()}",
        target=normalize_bot_username(target) if separator else None,
        arg=arg,
    )


def is_group_chat(chat_type: str) -> bool:
    return chat_type in GROUP_CHAT_TYPES


def thread_id_for_message(message: TelegramTextMessage) -> str:
    return str(message.chat_id)


def reply_to_message_id_for_response(message: TelegramTextMessage) -> int | None:
    if is_group_chat(message.chat_type):
        return message.message_id
    return None


def message_metadata(
    message: TelegramTextMessage, bot_name: str, trigger: str
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "telegram_bot": bot_name,
        "chat_type": message.chat_type,
        "from_id": message.from_id,
        "update_id": message.update_id,
        "message_id": message.message_id,
    }
    if is_group_chat(message.chat_type):
        metadata["reply_to_message_id"] = message.message_id
        metadata["telegram_trigger"] = trigger
    return metadata


def strip_bot_mentions(text: str, username: str | None) -> tuple[bool, str]:
    bot_username = normalize_bot_username(username)
    if bot_username is None:
        return False, text.strip()
    pattern = re.compile(rf"(?<!\w)@{re.escape(bot_username)}\b", re.IGNORECASE)
    mentioned = pattern.search(text) is not None
    if not mentioned:
        return False, text.strip()
    cleaned = pattern.sub(" ", text)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    return True, cleaned.strip()


def format_group_prompt(message: TelegramTextMessage, prompt: str) -> str:
    return f"Telegram group message from {sender_label(message)}:\n{prompt.strip()}"


def sender_label(message: TelegramTextMessage) -> str:
    display_name = " ".join(
        part for part in (message.from_first_name, message.from_last_name) if part
    ).strip()
    username = normalize_bot_username(message.from_username)
    if display_name and username:
        return f"{display_name} (@{username})"
    if display_name:
        return display_name
    if username:
        return f"@{username}"
    if message.from_id is not None:
        return f"Telegram user {message.from_id}"
    return "Unknown sender"


def _optional_str(raw: dict[str, Any] | None, key: str) -> str | None:
    if not isinstance(raw, dict):
        return None
    value = raw.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def format_for_telegram(text: str) -> list[str]:
    if not text:
        return [""]
    plain, entities = telegramify_markdown.convert(text)
    return [
        telegramify_markdown.entities_to_markdownv2(chunk_text, chunk_entities)
        for chunk_text, chunk_entities in telegramify_markdown.split_entities(
            plain, entities, max_utf16_len=MAX_TELEGRAM_MESSAGE
        )
    ]
