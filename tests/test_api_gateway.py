from fastapi.testclient import TestClient

from vikram import api
from vikram.gateway import EnqueuedEvent
from vikram.settings import VikramSettings
from vikram.telegram_config import TelegramBotConfig, TelegramConfig


def configured_settings(tmp_path) -> VikramSettings:
    return VikramSettings(
        _env_file=None,
        VIKRAM_MODEL_PROVIDER="ollama",
        VIKRAM_MODEL="test-model",
        VIKRAM_DB_PATH=tmp_path / "vikram.sqlite3",
        DBOS_SYSTEM_DATABASE_URL=f"sqlite:///{tmp_path / 'dbos.sqlite3'}",
    )


def configure_test_api(monkeypatch, tmp_path) -> None:
    api._agents.clear()
    monkeypatch.setattr(api, "_settings", configured_settings(tmp_path))


class FakeDispatcher:
    def __init__(self):
        self.messages = []

    async def enqueue_message(self, message):
        self.messages.append(message)
        return EnqueuedEvent(workflow_id="wf-1", status="queued")

    async def get_event_status(self, workflow_id):
        return {
            "workflow_id": workflow_id,
            "status": "SUCCESS",
            "result": {"output": "hello"},
            "error": None,
        }


def test_thread_message_endpoint_enqueues_message(monkeypatch, tmp_path):
    dispatcher = FakeDispatcher()
    configure_test_api(monkeypatch, tmp_path)
    monkeypatch.setattr(api, "_get_dispatcher", lambda: dispatcher)

    with TestClient(api.app) as client:
        response = client.post(
            "/threads/web/abc/messages",
            json={"prompt": "hello", "agent": "vikram"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "workflow_id": "wf-1",
        "thread_id": "web:abc",
        "status": "queued",
    }
    assert dispatcher.messages[0].interface == "web"
    assert dispatcher.messages[0].external_thread_id == "abc"


def test_thread_message_endpoint_rejects_cli_only_agent(monkeypatch, tmp_path):
    dispatcher = FakeDispatcher()
    configure_test_api(monkeypatch, tmp_path)
    monkeypatch.setattr(api, "_get_dispatcher", lambda: dispatcher)

    with TestClient(api.app) as client:
        response = client.post(
            "/threads/web/abc/messages",
            json={"prompt": "hello", "agent": "coder"},
        )

    assert response.status_code == 403
    assert "CLI-only" in response.json()["detail"]
    assert dispatcher.messages == []


def test_event_status_endpoint_returns_dispatcher_status(monkeypatch, tmp_path):
    dispatcher = FakeDispatcher()
    configure_test_api(monkeypatch, tmp_path)
    monkeypatch.setattr(api, "_get_dispatcher", lambda: dispatcher)

    with TestClient(api.app) as client:
        response = client.get("/events/wf-1")

    assert response.status_code == 200
    assert response.json()["status"] == "SUCCESS"
    assert response.json()["result"] == {"output": "hello"}


def test_telegram_webhook_rejects_bad_secret(monkeypatch, tmp_path):
    config = TelegramConfig(
        default_bot_name="vikram",
        bots={
            "vikram": TelegramBotConfig(
                name="vikram",
                default_agent="vikram",
                bot_token="token",
                webhook_secret="secret",
                allowed_chat_ids="123",
                api_base_url="https://api.telegram.org",
            )
        },
    )
    configure_test_api(monkeypatch, tmp_path)
    monkeypatch.setattr(api, "_get_telegram_config", lambda: config)

    with TestClient(api.app) as client:
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
            json={},
        )

    assert response.status_code == 403


def test_named_telegram_webhook_dispatches_to_selected_bot(monkeypatch, tmp_path):
    class FakeAdapter:
        def __init__(self):
            self.updates = []

        async def handle_update(self, update):
            self.updates.append(update)
            return type("Result", (), {"status": "queued", "workflow_id": "wf-1"})()

    adapter = FakeAdapter()
    config = TelegramConfig(
        default_bot_name="vikram",
        bots={
            "research": TelegramBotConfig(
                name="research",
                default_agent="research",
                bot_token="token",
                webhook_secret="secret",
                allowed_chat_ids="123",
                api_base_url="https://api.telegram.org",
            )
        },
    )
    configure_test_api(monkeypatch, tmp_path)
    monkeypatch.setattr(api, "_get_telegram_config", lambda: config)
    monkeypatch.setattr(api, "_get_telegram_adapter", lambda bot_name: adapter)

    with TestClient(api.app) as client:
        response = client.post(
            "/telegram/research/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
            json={"update_id": 1},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "queued", "workflow_id": "wf-1"}
    assert adapter.updates == [{"update_id": 1}]


def test_legacy_telegram_webhook_uses_default_bot(monkeypatch, tmp_path):
    class FakeAdapter:
        async def handle_update(self, update):
            return type("Result", (), {"status": "handled", "workflow_id": None})()

    config = TelegramConfig(
        default_bot_name="vikram",
        bots={
            "vikram": TelegramBotConfig(
                name="vikram",
                default_agent="vikram",
                bot_token="token",
                webhook_secret="secret",
                allowed_chat_ids="123",
                api_base_url="https://api.telegram.org",
            )
        },
    )
    called = []
    configure_test_api(monkeypatch, tmp_path)
    monkeypatch.setattr(api, "_get_telegram_config", lambda: config)
    monkeypatch.setattr(
        api,
        "_get_telegram_adapter",
        lambda bot_name: called.append(bot_name) or FakeAdapter(),
    )

    with TestClient(api.app) as client:
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
            json={"update_id": 1},
        )

    assert response.status_code == 200
    assert called == ["vikram"]
