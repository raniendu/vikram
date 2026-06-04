from pathlib import Path

import pytest
from structlog.testing import capture_logs

from vikram.gateway import (
    ConversationService,
    InboundMessage,
    ThreadStore,
    make_message_received_event,
)
from vikram.settings import VikramSettings


def test_thread_store_persists_thread_agent_and_history(tmp_path):
    store = ThreadStore(tmp_path / "vikram.sqlite3")

    thread = store.get_thread("telegram", "123", default_agent="vikram")
    assert thread.agent_name == "vikram"
    assert thread.message_history_json is None

    store.set_history(
        "telegram", "123", agent_name="alfred", message_history_json=b"[]"
    )
    updated = store.get_thread("telegram", "123", default_agent="vikram")

    assert updated.agent_name == "alfred"
    assert updated.message_history_json == b"[]"

    store.reset_history("telegram", "123")
    reset = store.get_thread("telegram", "123", default_agent="vikram")

    assert reset.agent_name == "alfred"
    assert reset.message_history_json is None


def test_thread_store_claims_telegram_updates_once(tmp_path):
    store = ThreadStore(tmp_path / "vikram.sqlite3")

    assert store.claim_telegram_update("bot-a", 42) is True
    assert store.claim_telegram_update("bot-a", 42) is False
    assert store.claim_telegram_update("bot-b", 42) is True


def test_message_received_event_uses_cloudevent_metadata():
    event = make_message_received_event(
        InboundMessage(
            interface="telegram",
            external_thread_id="123",
            prompt="hello",
            agent_name="vikram",
            default_agent="vikram",
            metadata={"chat_type": "private"},
        )
    )

    assert event["type"] == "vikram.message.received"
    assert event["source"] == "/interfaces/telegram/threads/123"
    assert event.get_data()["prompt"] == "hello"
    assert event.get_data()["agent_name"] == "vikram"
    assert event.get_data()["default_agent"] == "vikram"


@pytest.mark.asyncio
async def test_conversation_service_runs_agent_with_persisted_history(tmp_path):
    store = ThreadStore(tmp_path / "vikram.sqlite3")
    calls = []

    class FakeResult:
        output = "reply"

        def all_messages_json(self):
            return b"[]"

    class FakeAgent:
        async def run(self, prompt, *, message_history, conversation_id):
            calls.append((prompt, message_history, conversation_id))
            return FakeResult()

    service = ConversationService(
        settings=VikramSettings(_env_file=None),
        store=store,
        agent_factory=lambda name: FakeAgent(),
    )

    reply = await service.send_message(
        InboundMessage(
            interface="telegram",
            external_thread_id="123",
            prompt="hello",
            agent_name=None,
            default_agent="other",
            metadata={},
        )
    )

    assert reply.output == "reply"
    assert reply.agent_name == "other"
    assert calls == [("hello", [], "telegram:123")]
    assert (
        store.get_thread("telegram", "123", default_agent="other").message_history_json
        == b"[]"
    )


@pytest.mark.asyncio
async def test_conversation_service_rejects_cli_only_agent(tmp_path):
    store = ThreadStore(tmp_path / "vikram.sqlite3")
    service = ConversationService(
        settings=VikramSettings(_env_file=None),
        store=store,
    )

    with pytest.raises(RuntimeError, match="CLI-only"):
        await service.send_message(
            InboundMessage(
                interface="telegram",
                external_thread_id="123",
                prompt="hello",
                agent_name="coder",
                metadata={},
            )
        )


@pytest.mark.asyncio
async def test_conversation_service_appends_context_warning_near_limit(tmp_path):
    store = ThreadStore(tmp_path / "vikram.sqlite3")

    class FakeUsage:
        input_tokens = 90

    class FakeResult:
        output = "reply"

        def all_messages_json(self):
            return b'["stored-model-history"]'

        def usage(self):
            return FakeUsage()

    class FakeAgent:
        async def run(self, prompt, *, message_history, conversation_id):
            return FakeResult()

    service = ConversationService(
        settings=VikramSettings(
            _env_file=None,
            VIKRAM_CONTEXT_WINDOW_TOKENS=100,
            VIKRAM_CONTEXT_WARNING_RATIO=0.8,
        ),
        store=store,
        agent_factory=lambda name: FakeAgent(),
    )

    reply = await service.send_message(
        InboundMessage(
            interface="telegram",
            external_thread_id="123",
            prompt="hello",
            agent_name=None,
            default_agent="vikram",
            metadata={},
        )
    )

    assert reply.output.startswith("reply")
    assert "Context warning:" in reply.output
    assert "90%" in reply.output
    assert (
        store.get_thread("telegram", "123", default_agent="vikram").message_history_json
        == b'["stored-model-history"]'
    )
    assert (
        b"Context warning"
        not in store.get_thread(
            "telegram", "123", default_agent="vikram"
        ).message_history_json
    )


@pytest.mark.asyncio
async def test_conversation_service_logs_agent_lifecycle_without_content(tmp_path):
    store = ThreadStore(tmp_path / "vikram.sqlite3")

    class FakeResult:
        output = "sensitive reply"

        def all_messages_json(self):
            return b"[]"

    class FakeAgent:
        async def run(self, prompt, *, message_history, conversation_id):
            return FakeResult()

    service = ConversationService(
        settings=VikramSettings(_env_file=None),
        store=store,
        agent_factory=lambda name: FakeAgent(),
    )

    with capture_logs() as logs:
        reply = await service.send_message(
            InboundMessage(
                interface="telegram",
                external_thread_id="123",
                prompt="sensitive prompt",
                agent_name=None,
                metadata={"update_id": 42},
            )
        )

    assert reply.output == "sensitive reply"
    events = {entry["event"] for entry in logs}
    assert "agent_run_started" in events
    assert "agent_run_succeeded" in events
    assert "sensitive prompt" not in repr(logs)
    assert "sensitive reply" not in repr(logs)
