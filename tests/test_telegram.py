import httpx
import pytest
from structlog.testing import capture_logs

from vikram.gateway import EnqueuedEvent, ThreadStore
from vikram.settings import VikramSettings
from vikram.telegram import TelegramAdapter, format_for_telegram
from vikram.telegram_config import TelegramBotConfig


def expected(text: str) -> list[str]:
    return format_for_telegram(text)


async def ignore_chat_action(chat_id, action):
    pass


def test_format_for_telegram_converts_markdown_to_markdownv2():
    chunks = format_for_telegram(
        "**Bold heading**\n\n*   bullet one\n*   bullet two\n\n`code`"
    )
    assert len(chunks) == 1
    body = chunks[0]
    assert "*Bold heading*" in body
    assert "**" not in body
    assert "⦁ bullet one" in body
    assert "⦁ bullet two" in body
    assert "`code`" in body


def test_format_for_telegram_empty_input():
    assert format_for_telegram("") == [""]


def make_settings(**overrides):
    values = {
        "telegram_bot_token": "token",
        "telegram_webhook_secret": "secret",
        "telegram_allowed_chat_ids": "123",
    }
    values.update(overrides)
    return VikramSettings(_env_file=None, **values)


def make_bot_config(**overrides):
    values = {
        "name": "bot-a",
        "default_agent": "vikram",
        "bot_token": "token",
        "webhook_secret": "secret",
        "allowed_chat_ids": "123",
        "api_base_url": "https://api.telegram.org",
    }
    values.update(overrides)
    return TelegramBotConfig(**values)


def text_update(
    text,
    *,
    chat_id=123,
    update_id=1,
    chat_type="private",
    message_id=10,
    from_id=999,
    from_username=None,
    from_first_name=None,
    from_last_name=None,
    reply_to_message_id=None,
    reply_to_from_username=None,
):
    raw_from = {"id": from_id}
    if from_username is not None:
        raw_from["username"] = from_username
    if from_first_name is not None:
        raw_from["first_name"] = from_first_name
    if from_last_name is not None:
        raw_from["last_name"] = from_last_name
    raw_message = {
        "message_id": message_id,
        "text": text,
        "chat": {"id": chat_id, "type": chat_type},
        "from": raw_from,
    }
    if reply_to_message_id is not None:
        raw_message["reply_to_message"] = {
            "message_id": reply_to_message_id,
            "from": {"username": reply_to_from_username},
        }
    return {
        "update_id": update_id,
        "message": raw_message,
    }


@pytest.mark.asyncio
async def test_telegram_enqueues_allowed_text_message(tmp_path):
    enqueued = []
    sent = []
    actions = []
    events = []

    async def enqueue(message):
        enqueued.append(message)
        events.append("enqueue")
        return EnqueuedEvent(workflow_id="wf-1", status="queued")

    async def send_text(chat_id, text, reply_to_message_id=None):
        sent.append((chat_id, text, reply_to_message_id))

    async def send_chat_action(chat_id, action):
        actions.append((chat_id, action))
        events.append("chat_action")

    adapter = TelegramAdapter(
        settings=make_settings(),
        store=ThreadStore(tmp_path / "vikram.sqlite3"),
        enqueue_message=enqueue,
        send_text=send_text,
        send_chat_action=send_chat_action,
    )

    result = await adapter.handle_update(text_update("hello"))

    assert result.status == "queued"
    assert result.workflow_id == "wf-1"
    assert enqueued[0].interface == "telegram"
    assert enqueued[0].external_thread_id == "123"
    assert enqueued[0].prompt == "hello"
    assert sent == []
    assert actions == [(123, "typing")]
    assert events == ["chat_action", "enqueue"]


@pytest.mark.asyncio
async def test_telegram_enqueues_bot_scoped_message_with_default_agent(tmp_path):
    enqueued = []

    async def enqueue(message):
        enqueued.append(message)
        return EnqueuedEvent(workflow_id="wf-1", status="queued")

    adapter = TelegramAdapter(
        settings=make_settings(),
        bot=make_bot_config(name="bot-a", default_agent="research"),
        store=ThreadStore(tmp_path / "vikram.sqlite3"),
        enqueue_message=enqueue,
        send_chat_action=ignore_chat_action,
    )

    result = await adapter.handle_update(text_update("hello"))

    assert result.status == "queued"
    assert enqueued[0].interface == "telegram:bot-a"
    assert enqueued[0].external_thread_id == "123"
    assert enqueued[0].default_agent == "research"
    assert enqueued[0].metadata["telegram_bot"] == "bot-a"


@pytest.mark.asyncio
async def test_telegram_logs_update_lifecycle_without_message_text(tmp_path):
    async def enqueue(message):
        return EnqueuedEvent(workflow_id="wf-1", status="queued")

    adapter = TelegramAdapter(
        settings=make_settings(),
        store=ThreadStore(tmp_path / "vikram.sqlite3"),
        enqueue_message=enqueue,
        send_text=None,
        send_chat_action=ignore_chat_action,
    )

    with capture_logs() as logs:
        result = await adapter.handle_update(text_update("do not log this text"))

    assert result.status == "queued"
    events = {entry["event"] for entry in logs}
    assert "telegram_update_received" in events
    assert "telegram_message_enqueued" in events
    assert "do not log this text" not in repr(logs)


@pytest.mark.asyncio
async def test_telegram_rejects_unknown_chat(tmp_path):
    sent = []

    async def send_text(chat_id, text, reply_to_message_id=None):
        sent.append((chat_id, text, reply_to_message_id))

    adapter = TelegramAdapter(
        settings=make_settings(),
        store=ThreadStore(tmp_path / "vikram.sqlite3"),
        enqueue_message=None,
        send_text=send_text,
    )

    result = await adapter.handle_update(text_update("hello", chat_id=999))

    assert result.status == "rejected"
    assert sent == [(999, chunk, None) for chunk in expected("This bot is private.")]


@pytest.mark.asyncio
async def test_telegram_rejects_allowed_non_text_message(tmp_path):
    sent = []

    async def send_text(chat_id, text, reply_to_message_id=None):
        sent.append((chat_id, text, reply_to_message_id))

    adapter = TelegramAdapter(
        settings=make_settings(),
        store=ThreadStore(tmp_path / "vikram.sqlite3"),
        enqueue_message=None,
        send_text=send_text,
    )

    result = await adapter.handle_update(
        {
            "update_id": 2,
            "message": {
                "message_id": 11,
                "chat": {"id": 123, "type": "private"},
                "photo": [{"file_id": "abc"}],
            },
        }
    )

    assert result.status == "rejected"
    assert sent == [
        (123, chunk, None) for chunk in expected("Text messages only for now.")
    ]


@pytest.mark.asyncio
async def test_telegram_reset_command_clears_history(tmp_path):
    store = ThreadStore(tmp_path / "vikram.sqlite3")
    store.set_history(
        "telegram", "123", agent_name="vikram", message_history_json=b"[]"
    )
    sent = []

    async def send_text(chat_id, text, reply_to_message_id=None):
        sent.append((chat_id, text, reply_to_message_id))

    adapter = TelegramAdapter(
        settings=make_settings(),
        store=store,
        enqueue_message=None,
        send_text=send_text,
    )

    result = await adapter.handle_update(text_update("/reset"))

    assert result.status == "handled"
    assert (
        store.get_thread("telegram", "123", default_agent="vikram").message_history_json
        is None
    )
    assert sent == [(123, chunk, None) for chunk in expected("Conversation reset.")]


@pytest.mark.asyncio
async def test_telegram_agent_command_persists_existing_spec(tmp_path):
    store = ThreadStore(tmp_path / "vikram.sqlite3")
    sent = []

    async def send_text(chat_id, text, reply_to_message_id=None):
        sent.append((chat_id, text, reply_to_message_id))

    adapter = TelegramAdapter(
        settings=make_settings(),
        store=store,
        enqueue_message=None,
        send_text=send_text,
    )

    result = await adapter.handle_update(text_update("/agent vikram"))

    assert result.status == "handled"
    assert (
        store.get_thread("telegram", "123", default_agent="alfred").agent_name
        == "vikram"
    )
    assert sent == [(123, chunk, None) for chunk in expected("Agent set to vikram.")]


@pytest.mark.asyncio
async def test_telegram_agent_command_rejects_cli_only_spec(tmp_path):
    store = ThreadStore(tmp_path / "vikram.sqlite3")
    sent = []

    async def send_text(chat_id, text, reply_to_message_id=None):
        sent.append((chat_id, text, reply_to_message_id))

    adapter = TelegramAdapter(
        settings=make_settings(),
        store=store,
        enqueue_message=None,
        send_text=send_text,
    )

    result = await adapter.handle_update(text_update("/agent coder"))

    assert result.status == "handled"
    assert (
        store.get_thread("telegram", "123", default_agent="alfred").agent_name
        == "alfred"
    )
    assert sent == [
        (123, chunk, None)
        for chunk in expected("Agent coder is only available in the local CLI.")
    ]


@pytest.mark.asyncio
async def test_telegram_agent_command_persists_in_bot_scoped_thread(tmp_path):
    store = ThreadStore(tmp_path / "vikram.sqlite3")
    sent = []

    async def send_text(chat_id, text, reply_to_message_id=None):
        sent.append((chat_id, text, reply_to_message_id))

    adapter = TelegramAdapter(
        settings=make_settings(),
        bot=make_bot_config(name="bot-a", default_agent="research"),
        store=store,
        enqueue_message=None,
        send_text=send_text,
    )

    result = await adapter.handle_update(text_update("/agent vikram"))

    assert result.status == "handled"
    assert (
        store.get_thread("telegram:bot-a", "123", default_agent="research").agent_name
        == "vikram"
    )
    assert sent == [(123, chunk, None) for chunk in expected("Agent set to vikram.")]


@pytest.mark.asyncio
async def test_telegram_ignores_group_text_without_trigger(tmp_path):
    enqueued = []
    sent = []

    async def enqueue(message):
        enqueued.append(message)
        return EnqueuedEvent(workflow_id="wf-1", status="queued")

    async def send_text(chat_id, text, reply_to_message_id=None):
        sent.append((chat_id, text, reply_to_message_id))

    adapter = TelegramAdapter(
        settings=make_settings(),
        bot=make_bot_config(
            name="vikram",
            allowed_chat_ids="-100123",
            username="VikramBot",
        ),
        store=ThreadStore(tmp_path / "vikram.sqlite3"),
        enqueue_message=enqueue,
        send_text=send_text,
    )

    result = await adapter.handle_update(
        text_update("normal group chatter", chat_id=-100123, chat_type="supergroup")
    )

    assert result.status == "ignored"
    assert enqueued == []
    assert sent == []


@pytest.mark.asyncio
async def test_telegram_enqueues_group_mention_with_clean_prompt_and_sender_context(
    tmp_path,
):
    enqueued = []

    async def enqueue(message):
        enqueued.append(message)
        return EnqueuedEvent(workflow_id="wf-1", status="queued")

    adapter = TelegramAdapter(
        settings=make_settings(),
        bot=make_bot_config(
            name="vikram",
            allowed_chat_ids="-100123",
            username="VikramBot",
        ),
        store=ThreadStore(tmp_path / "vikram.sqlite3"),
        enqueue_message=enqueue,
        send_chat_action=ignore_chat_action,
    )

    result = await adapter.handle_update(
        text_update(
            "@VikramBot who is president?",
            chat_id=-100123,
            chat_type="group",
            message_id=55,
            from_username="alex",
            from_first_name="Alex",
            from_last_name="Kim",
        )
    )

    assert result.status == "queued"
    assert enqueued[0].external_thread_id == "-100123"
    assert (
        enqueued[0].prompt
        == "Telegram group message from Alex Kim (@alex):\nwho is president?"
    )
    assert enqueued[0].metadata["reply_to_message_id"] == 55
    assert enqueued[0].metadata["telegram_trigger"] == "mention"


@pytest.mark.asyncio
async def test_telegram_enqueues_group_reply_to_bot_with_reply_metadata(tmp_path):
    enqueued = []

    async def enqueue(message):
        enqueued.append(message)
        return EnqueuedEvent(workflow_id="wf-1", status="queued")

    adapter = TelegramAdapter(
        settings=make_settings(),
        bot=make_bot_config(
            name="vikram",
            allowed_chat_ids="-100123",
            username="VikramBot",
        ),
        store=ThreadStore(tmp_path / "vikram.sqlite3"),
        enqueue_message=enqueue,
        send_chat_action=ignore_chat_action,
    )

    result = await adapter.handle_update(
        text_update(
            "yes",
            chat_id=-100123,
            chat_type="supergroup",
            message_id=56,
            from_first_name="Rani",
            reply_to_message_id=11,
            reply_to_from_username="VikramBot",
        )
    )

    assert result.status == "queued"
    assert enqueued[0].prompt == "Telegram group message from Rani:\nyes"
    assert enqueued[0].metadata["reply_to_message_id"] == 56
    assert enqueued[0].metadata["telegram_trigger"] == "reply"


@pytest.mark.asyncio
async def test_telegram_ignores_group_command_addressed_to_another_bot(tmp_path):
    sent = []

    async def send_text(chat_id, text, reply_to_message_id=None):
        sent.append((chat_id, text, reply_to_message_id))

    adapter = TelegramAdapter(
        settings=make_settings(),
        bot=make_bot_config(
            name="vikram",
            allowed_chat_ids="-100123",
            username="VikramBot",
        ),
        store=ThreadStore(tmp_path / "vikram.sqlite3"),
        enqueue_message=None,
        send_text=send_text,
    )

    result = await adapter.handle_update(
        text_update("/help@OtherBot", chat_id=-100123, chat_type="supergroup")
    )

    assert result.status == "ignored"
    assert sent == []


@pytest.mark.asyncio
async def test_telegram_group_reset_command_requires_chat_admin(tmp_path):
    store = ThreadStore(tmp_path / "vikram.sqlite3")
    store.set_history(
        "telegram:vikram",
        "-100123",
        agent_name="vikram",
        message_history_json=b"[]",
    )
    sent = []

    async def send_text(chat_id, text, reply_to_message_id=None):
        sent.append((chat_id, text, reply_to_message_id))

    async def get_chat_member(chat_id, user_id):
        return {"status": "member"}

    adapter = TelegramAdapter(
        settings=make_settings(),
        bot=make_bot_config(
            name="vikram",
            allowed_chat_ids="-100123",
            username="VikramBot",
        ),
        store=store,
        enqueue_message=None,
        send_text=send_text,
        get_chat_member=get_chat_member,
    )

    result = await adapter.handle_update(
        text_update(
            "/reset@VikramBot",
            chat_id=-100123,
            chat_type="supergroup",
            message_id=57,
        )
    )

    assert result.status == "handled"
    assert (
        store.get_thread(
            "telegram:vikram", "-100123", default_agent="vikram"
        ).message_history_json
        == b"[]"
    )
    assert sent == [
        (
            -100123,
            chunk,
            57,
        )
        for chunk in expected("Only Telegram chat admins can use /reset in groups.")
    ]


@pytest.mark.asyncio
async def test_telegram_group_reset_command_allows_chat_admin(tmp_path):
    store = ThreadStore(tmp_path / "vikram.sqlite3")
    store.set_history(
        "telegram:vikram",
        "-100123",
        agent_name="vikram",
        message_history_json=b"[]",
    )
    sent = []

    async def send_text(chat_id, text, reply_to_message_id=None):
        sent.append((chat_id, text, reply_to_message_id))

    async def get_chat_member(chat_id, user_id):
        return {"status": "administrator"}

    adapter = TelegramAdapter(
        settings=make_settings(),
        bot=make_bot_config(
            name="vikram",
            allowed_chat_ids="-100123",
            username="VikramBot",
        ),
        store=store,
        enqueue_message=None,
        send_text=send_text,
        get_chat_member=get_chat_member,
    )

    result = await adapter.handle_update(
        text_update(
            "/reset@VikramBot",
            chat_id=-100123,
            chat_type="supergroup",
            message_id=58,
        )
    )

    assert result.status == "handled"
    assert (
        store.get_thread(
            "telegram:vikram", "-100123", default_agent="vikram"
        ).message_history_json
        is None
    )
    assert sent == [(-100123, chunk, 58) for chunk in expected("Conversation reset.")]


@pytest.mark.asyncio
async def test_telegram_group_agent_command_fails_closed_when_admin_check_fails(
    tmp_path,
):
    sent = []

    async def send_text(chat_id, text, reply_to_message_id=None):
        sent.append((chat_id, text, reply_to_message_id))

    async def get_chat_member(chat_id, user_id):
        raise RuntimeError("telegram unavailable")

    adapter = TelegramAdapter(
        settings=make_settings(),
        bot=make_bot_config(
            name="vikram",
            allowed_chat_ids="-100123",
            username="VikramBot",
        ),
        store=ThreadStore(tmp_path / "vikram.sqlite3"),
        enqueue_message=None,
        send_text=send_text,
        get_chat_member=get_chat_member,
    )

    result = await adapter.handle_update(
        text_update(
            "/agent@VikramBot vikram",
            chat_id=-100123,
            chat_type="supergroup",
            message_id=59,
        )
    )

    assert result.status == "handled"
    assert sent == [
        (
            -100123,
            chunk,
            59,
        )
        for chunk in expected(
            "I couldn't verify Telegram admin status. Try again later."
        )
    ]


@pytest.mark.asyncio
async def test_telegram_group_agent_command_allows_admin_to_select_vikram(tmp_path):
    store = ThreadStore(tmp_path / "vikram.sqlite3")
    sent = []

    async def send_text(chat_id, text, reply_to_message_id=None):
        sent.append((chat_id, text, reply_to_message_id))

    async def get_chat_member(chat_id, user_id):
        return {"status": "creator"}

    adapter = TelegramAdapter(
        settings=make_settings(),
        bot=make_bot_config(
            name="vikram",
            allowed_chat_ids="-100123",
            username="VikramBot",
        ),
        store=store,
        enqueue_message=None,
        send_text=send_text,
        get_chat_member=get_chat_member,
    )

    result = await adapter.handle_update(
        text_update(
            "/agent@VikramBot vikram",
            chat_id=-100123,
            chat_type="supergroup",
            message_id=61,
        )
    )

    assert result.status == "handled"
    assert (
        store.get_thread(
            "telegram:vikram", "-100123", default_agent="vikram"
        ).agent_name
        == "vikram"
    )
    assert sent == [(-100123, chunk, 61) for chunk in expected("Agent set to vikram.")]


@pytest.mark.asyncio
async def test_telegram_group_admin_check_logs_do_not_include_bot_token(tmp_path):
    sent = []

    async def send_text(chat_id, text, reply_to_message_id=None):
        sent.append((chat_id, text, reply_to_message_id))

    async def get_chat_member(chat_id, user_id):
        request = httpx.Request(
            "POST",
            "https://api.telegram.org/botsecret-token/getChatMember",
        )
        raise httpx.RequestError("connection failed", request=request)

    adapter = TelegramAdapter(
        settings=make_settings(),
        bot=make_bot_config(
            name="vikram",
            bot_token="secret-token",
            allowed_chat_ids="-100123",
            username="VikramBot",
        ),
        store=ThreadStore(tmp_path / "vikram.sqlite3"),
        enqueue_message=None,
        send_text=send_text,
        get_chat_member=get_chat_member,
    )

    with capture_logs() as logs:
        result = await adapter.handle_update(
            text_update(
                "/reset@VikramBot",
                chat_id=-100123,
                chat_type="supergroup",
                message_id=60,
            )
        )

    assert result.status == "handled"
    assert "secret-token" not in repr(logs)


@pytest.mark.asyncio
async def test_fetch_chat_member_http_errors_do_not_include_bot_token(
    monkeypatch, tmp_path
):
    class FailingClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, json):
            request = httpx.Request(
                "POST",
                f"https://api.telegram.org{url}",
            )
            raise httpx.HTTPStatusError(
                "server error for https://api.telegram.org/botsecret-token/getChatMember",
                request=request,
                response=httpx.Response(500, request=request),
            )

    monkeypatch.setattr(httpx, "AsyncClient", FailingClient)
    adapter = TelegramAdapter(
        settings=make_settings(),
        bot=make_bot_config(
            name="vikram",
            bot_token="secret-token",
            allowed_chat_ids="-100123",
            username="VikramBot",
        ),
        store=ThreadStore(tmp_path / "vikram.sqlite3"),
        enqueue_message=None,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await adapter._fetch_chat_member(-100123, 999)

    assert "secret-token" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_telegram_group_logs_do_not_include_prompt_or_sender_names(tmp_path):
    async def enqueue(message):
        return EnqueuedEvent(workflow_id="wf-1", status="queued")

    adapter = TelegramAdapter(
        settings=make_settings(),
        bot=make_bot_config(
            name="vikram",
            allowed_chat_ids="-100123",
            username="VikramBot",
        ),
        store=ThreadStore(tmp_path / "vikram.sqlite3"),
        enqueue_message=enqueue,
        send_text=None,
        send_chat_action=ignore_chat_action,
    )

    with capture_logs() as logs:
        result = await adapter.handle_update(
            text_update(
                "@VikramBot do not log this text",
                chat_id=-100123,
                chat_type="group",
                from_username="alex",
                from_first_name="Alex",
                from_last_name="Kim",
            )
        )

    assert result.status == "queued"
    log_output = repr(logs)
    assert "do not log this text" not in log_output
    assert "Alex" not in log_output
    assert "alex" not in log_output
