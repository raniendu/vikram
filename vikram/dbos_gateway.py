from __future__ import annotations

from pathlib import Path
from typing import Any

from dbos import DBOS, Queue

from vikram.gateway import (
    EnqueuedEvent,
    InboundMessage,
    ThreadStore,
    cloud_event_from_dict,
    cloud_event_to_dict,
    inbound_message_from_event,
    make_message_received_event,
    make_reply_requested_event,
)
from vikram.logging import get_logger, safe_database_url, safe_metadata, thread_hash
from vikram.settings import VikramSettings
from vikram.telegram import TelegramAdapter
from vikram.telegram_config import load_telegram_config

INBOUND_QUEUE = Queue("vikram-inbound", concurrency=1, polling_interval_sec=0.1)
OUTBOUND_QUEUE = Queue("vikram-outbound", concurrency=1, polling_interval_sec=0.1)
TELEGRAM_FAILURE_REPLY = (
    "I hit an internal error while processing that. The issue has been logged."
)
logger = get_logger(__name__)

_configured = False


class EventDispatcher:
    async def enqueue_message(self, message: InboundMessage) -> EnqueuedEvent:
        event = make_message_received_event(message)
        logger.info(
            "dbos_enqueue_inbound_started",
            interface=message.interface,
            thread_hash=thread_hash(message.interface, message.external_thread_id),
            agent=message.agent_name,
            prompt_length=len(message.prompt),
            **safe_metadata(message.metadata),
        )
        handle = await INBOUND_QUEUE.enqueue_async(
            process_inbound_message_event,
            cloud_event_to_dict(event),
        )
        logger.info(
            "dbos_enqueue_inbound_succeeded",
            workflow_id=handle.workflow_id,
            interface=message.interface,
            thread_hash=thread_hash(message.interface, message.external_thread_id),
            **safe_metadata(message.metadata),
        )
        return EnqueuedEvent(workflow_id=handle.workflow_id, status="queued")

    async def get_event_status(self, workflow_id: str) -> dict[str, Any]:
        status = await DBOS.get_workflow_status_async(workflow_id)
        if status is None:
            return {
                "workflow_id": workflow_id,
                "status": "NOT_FOUND",
                "result": None,
                "error": None,
            }
        return {
            "workflow_id": status.workflow_id,
            "status": status.status,
            "result": status.output,
            "error": str(status.error) if status.error else None,
        }


def configure_dbos(settings: VikramSettings) -> None:
    global _configured
    if _configured:
        return
    _ensure_sqlite_parent(settings.effective_dbos_system_database_url)
    DBOS(
        config={
            "name": "vikram",
            "system_database_url": settings.effective_dbos_system_database_url,
            "run_admin_server": False,
        }
    )
    logger.info(
        "dbos_configured",
        database_url=safe_database_url(settings.effective_dbos_system_database_url),
    )
    _configured = True


def launch_dbos(settings: VikramSettings) -> None:
    configure_dbos(settings)
    DBOS.launch()
    logger.info("dbos_launched")


def shutdown_dbos() -> None:
    global _configured
    DBOS.destroy()
    _configured = False
    logger.info("dbos_shutdown")


@DBOS.workflow(name="vikram_process_inbound_message")
async def process_inbound_message_event(event_dict: dict[str, Any]) -> dict[str, Any]:
    from vikram.gateway import ConversationService

    event = cloud_event_from_dict(event_dict)
    message = inbound_message_from_event(event)
    log = logger.bind(
        interface=message.interface,
        thread_hash=thread_hash(message.interface, message.external_thread_id),
        agent=message.agent_name,
        prompt_length=len(message.prompt),
        **safe_metadata(message.metadata),
    )
    log.info("dbos_inbound_workflow_started")
    settings = VikramSettings()
    store = ThreadStore(settings.vikram_db_path)
    try:
        reply = await ConversationService(settings=settings, store=store).send_message(
            message
        )
    except Exception as exc:
        log.exception(
            "dbos_inbound_workflow_failed",
            error_type=type(exc).__name__,
        )
        delivered = await _send_processing_failure_reply(message)
        log.info(
            "dbos_processing_failure_reply_attempted",
            delivered=delivered,
        )
        raise
    reply_event = make_reply_requested_event(message, reply)
    if reply.interface == "telegram" or reply.interface.startswith("telegram:"):
        handle = await OUTBOUND_QUEUE.enqueue_async(
            deliver_reply_event,
            cloud_event_to_dict(reply_event),
        )
        log.info(
            "dbos_outbound_enqueued",
            outbound_workflow_id=handle.workflow_id,
            output_length=len(reply.output),
        )
    log.info("dbos_inbound_workflow_succeeded", output_length=len(reply.output))
    return {
        "agent": reply.agent_name,
        "output": reply.output,
        "thread_id": f"{reply.interface}:{reply.external_thread_id}",
    }


@DBOS.workflow(name="vikram_deliver_reply")
async def deliver_reply_event(event_dict: dict[str, Any]) -> dict[str, Any]:
    event = cloud_event_from_dict(event_dict)
    data = event.get_data()
    if not isinstance(data, dict):
        raise ValueError("Reply event data must be an object")
    bot_name = _telegram_bot_name(data)
    if bot_name is None:
        logger.info(
            "dbos_reply_delivery_skipped",
            interface=data.get("interface"),
            reason="unsupported_interface",
        )
        return {"delivered": False, "reason": "unsupported interface"}
    logger.info(
        "dbos_reply_delivery_started",
        interface=data["interface"],
        thread_hash=thread_hash(
            str(data["interface"]), str(data["external_thread_id"])
        ),
        agent=data.get("agent_name"),
        output_length=len(str(data["output"])),
        **safe_metadata(data.get("metadata")),
    )
    await send_telegram_reply(
        bot_name,
        int(data["external_thread_id"]),
        str(data["output"]),
        reply_to_message_id=_reply_to_message_id(data.get("metadata")),
    )
    logger.info(
        "dbos_reply_delivery_succeeded",
        interface=data["interface"],
        thread_hash=thread_hash(
            str(data["interface"]), str(data["external_thread_id"])
        ),
        **safe_metadata(data.get("metadata")),
    )
    return {"delivered": True}


@DBOS.step(name="vikram_send_telegram_reply", retries_allowed=True, max_attempts=3)
async def send_telegram_reply(
    bot_name: str,
    chat_id: int,
    text: str,
    reply_to_message_id: int | None = None,
) -> None:
    settings = VikramSettings()
    config = load_telegram_config(
        settings.spec_root,
        default_agent=settings.default_agent,
    )
    adapter = TelegramAdapter(
        settings=settings,
        bot=config.get_bot(bot_name),
        store=ThreadStore(settings.vikram_db_path),
        enqueue_message=None,
    )
    await adapter.send_message(chat_id, text, reply_to_message_id=reply_to_message_id)


async def _send_processing_failure_reply(message: InboundMessage) -> bool:
    bot_name = _telegram_bot_name(
        {"interface": message.interface, "metadata": message.metadata}
    )
    if bot_name is None:
        return False
    log = logger.bind(
        interface=message.interface,
        thread_hash=thread_hash(message.interface, message.external_thread_id),
        **safe_metadata(message.metadata),
    )
    try:
        await send_telegram_reply(
            bot_name,
            int(message.external_thread_id),
            TELEGRAM_FAILURE_REPLY,
            reply_to_message_id=_reply_to_message_id(message.metadata),
        )
    except Exception:
        log.exception("dbos_processing_failure_reply_failed")
        return False
    log.info("dbos_processing_failure_reply_delivered")
    return True


def _telegram_bot_name(data: dict[str, Any]) -> str | None:
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        bot_name = metadata.get("telegram_bot")
        if isinstance(bot_name, str) and bot_name:
            return bot_name
    interface = str(data.get("interface", ""))
    if interface == "telegram":
        settings = VikramSettings()
        config = load_telegram_config(
            settings.spec_root,
            default_agent=settings.default_agent,
        )
        return config.default_bot_name
    if interface.startswith("telegram:"):
        return interface.split(":", 1)[1]
    return None


def _reply_to_message_id(metadata: Any) -> int | None:
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("reply_to_message_id")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ensure_sqlite_parent(database_url: str) -> None:
    prefix = "sqlite:///"
    if database_url.startswith(prefix):
        Path(database_url[len(prefix) :]).parent.mkdir(parents=True, exist_ok=True)
