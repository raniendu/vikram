from inspect import unwrap
from types import SimpleNamespace

import pytest

from vikram.dbos_gateway import (
    TELEGRAM_FAILURE_REPLY,
    _send_processing_failure_reply,
    deliver_reply_event,
    process_inbound_message_event,
)
from vikram.gateway import (
    ConversationReply,
    InboundMessage,
    cloud_event_to_dict,
    make_message_received_event,
    make_reply_requested_event,
)


@pytest.mark.asyncio
async def test_processing_failure_reply_is_sent_for_telegram(monkeypatch):
    sent = []

    async def fake_send_telegram_reply(
        bot_name, chat_id, text, reply_to_message_id=None
    ):
        sent.append((bot_name, chat_id, text, reply_to_message_id))

    monkeypatch.setattr(
        "vikram.dbos_gateway.send_telegram_reply", fake_send_telegram_reply
    )

    delivered = await _send_processing_failure_reply(
        InboundMessage(
            interface="telegram",
            external_thread_id="123",
            prompt="do not echo",
            agent_name=None,
            default_agent="vikram",
            metadata={"telegram_bot": "vikram", "update_id": 42},
        )
    )

    assert delivered is True
    assert sent == [("vikram", 123, TELEGRAM_FAILURE_REPLY, None)]
    assert "do not echo" not in TELEGRAM_FAILURE_REPLY


@pytest.mark.asyncio
async def test_inbound_workflow_enqueues_replies_for_named_telegram_bot(monkeypatch):
    outbound_events = []

    class SucceedingConversationService:
        def __init__(self, *, settings, store):
            pass

        async def send_message(self, message):
            return ConversationReply(
                interface=message.interface,
                external_thread_id=message.external_thread_id,
                agent_name="vikram",
                output="hello from vikram",
            )

    class FakeOutboundQueue:
        async def enqueue_async(self, workflow, event_dict):
            outbound_events.append((workflow, event_dict))
            return SimpleNamespace(workflow_id="outbound-1")

    monkeypatch.setattr(
        "vikram.gateway.ConversationService", SucceedingConversationService
    )
    monkeypatch.setattr("vikram.dbos_gateway.OUTBOUND_QUEUE", FakeOutboundQueue())
    message = InboundMessage(
        interface="telegram:vikram",
        external_thread_id="123",
        prompt="hello",
        agent_name=None,
        default_agent="vikram",
        metadata={"telegram_bot": "vikram", "update_id": 42},
    )

    result = await unwrap(process_inbound_message_event)(
        cloud_event_to_dict(make_message_received_event(message))
    )

    assert result["output"] == "hello from vikram"
    assert len(outbound_events) == 1
    _, event_dict = outbound_events[0]
    assert event_dict["data"]["interface"] == "telegram:vikram"
    assert event_dict["data"]["metadata"]["telegram_bot"] == "vikram"


@pytest.mark.asyncio
async def test_processing_failure_reply_is_skipped_for_non_telegram(monkeypatch):
    sent = []

    async def fake_send_telegram_reply(
        bot_name, chat_id, text, reply_to_message_id=None
    ):
        sent.append((bot_name, chat_id, text, reply_to_message_id))

    monkeypatch.setattr(
        "vikram.dbos_gateway.send_telegram_reply", fake_send_telegram_reply
    )

    delivered = await _send_processing_failure_reply(
        InboundMessage(
            interface="manual",
            external_thread_id="123",
            prompt="hello",
            agent_name=None,
            default_agent=None,
            metadata={},
        )
    )

    assert delivered is False
    assert sent == []


@pytest.mark.asyncio
async def test_processing_failure_reply_send_errors_do_not_escape(monkeypatch):
    async def fake_send_telegram_reply(
        bot_name, chat_id, text, reply_to_message_id=None
    ):
        raise RuntimeError("telegram send failed")

    monkeypatch.setattr(
        "vikram.dbos_gateway.send_telegram_reply", fake_send_telegram_reply
    )

    delivered = await _send_processing_failure_reply(
        InboundMessage(
            interface="telegram",
            external_thread_id="123",
            prompt="hello",
            agent_name=None,
            default_agent="vikram",
            metadata={"telegram_bot": "vikram"},
        )
    )

    assert delivered is False


@pytest.mark.asyncio
async def test_inbound_workflow_sends_failure_reply_before_reraising(monkeypatch):
    sent = []

    class FailingConversationService:
        def __init__(self, *, settings, store):
            pass

        async def send_message(self, message):
            raise RuntimeError("agent failed")

    async def fake_send_telegram_reply(
        bot_name, chat_id, text, reply_to_message_id=None
    ):
        sent.append((bot_name, chat_id, text, reply_to_message_id))

    monkeypatch.setattr(
        "vikram.gateway.ConversationService", FailingConversationService
    )
    monkeypatch.setattr(
        "vikram.dbos_gateway.send_telegram_reply", fake_send_telegram_reply
    )
    message = InboundMessage(
        interface="telegram",
        external_thread_id="123",
        prompt="do not echo",
        agent_name=None,
        default_agent="vikram",
        metadata={"telegram_bot": "vikram", "update_id": 42},
    )

    with pytest.raises(RuntimeError, match="agent failed"):
        await unwrap(process_inbound_message_event)(
            cloud_event_to_dict(make_message_received_event(message))
        )

    assert sent == [("vikram", 123, TELEGRAM_FAILURE_REPLY, None)]


@pytest.mark.asyncio
async def test_reply_delivery_preserves_telegram_reply_to_message_id(monkeypatch):
    sent = []

    async def fake_send_telegram_reply(
        bot_name, chat_id, text, reply_to_message_id=None
    ):
        sent.append((bot_name, chat_id, text, reply_to_message_id))

    monkeypatch.setattr(
        "vikram.dbos_gateway.send_telegram_reply", fake_send_telegram_reply
    )
    message = InboundMessage(
        interface="telegram:vikram",
        external_thread_id="-100123",
        prompt="hello",
        agent_name=None,
        default_agent="vikram",
        metadata={
            "telegram_bot": "vikram",
            "reply_to_message_id": 55,
            "update_id": 42,
        },
    )
    reply = ConversationReply(
        interface="telegram:vikram",
        external_thread_id="-100123",
        agent_name="vikram",
        output="hello back",
    )

    result = await unwrap(deliver_reply_event)(
        cloud_event_to_dict(make_reply_requested_event(message, reply))
    )

    assert result == {"delivered": True}
    assert sent == [("vikram", -100123, "hello back", 55)]


@pytest.mark.asyncio
async def test_processing_failure_reply_preserves_telegram_reply_to_message_id(
    monkeypatch,
):
    sent = []

    async def fake_send_telegram_reply(
        bot_name, chat_id, text, reply_to_message_id=None
    ):
        sent.append((bot_name, chat_id, text, reply_to_message_id))

    monkeypatch.setattr(
        "vikram.dbos_gateway.send_telegram_reply", fake_send_telegram_reply
    )

    delivered = await _send_processing_failure_reply(
        InboundMessage(
            interface="telegram:vikram",
            external_thread_id="-100123",
            prompt="do not echo",
            agent_name=None,
            default_agent="vikram",
            metadata={"telegram_bot": "vikram", "reply_to_message_id": 55},
        )
    )

    assert delivered is True
    assert sent == [("vikram", -100123, TELEGRAM_FAILURE_REPLY, 55)]
