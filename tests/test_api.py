from fastapi.testclient import TestClient

from vikram import api
from vikram.api import app
from vikram.settings import VikramSettings


def configure_test_api(monkeypatch, tmp_path):
    api._agents.clear()
    monkeypatch.setattr(
        api,
        "_settings",
        VikramSettings(
            _env_file=None,
            VIKRAM_MODEL_PROVIDER="ollama",
            VIKRAM_MODEL="test-model",
            VIKRAM_DB_PATH=tmp_path / "vikram.sqlite3",
            DBOS_SYSTEM_DATABASE_URL=f"sqlite:///{tmp_path / 'dbos.sqlite3'}",
        ),
    )


def test_healthz_returns_ok(monkeypatch, tmp_path):
    configure_test_api(monkeypatch, tmp_path)

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_chat_unknown_agent_returns_404(monkeypatch, tmp_path):
    configure_test_api(monkeypatch, tmp_path)

    with TestClient(app) as client:
        response = client.post("/chat", json={"prompt": "hi", "agent": "missing"})

    assert response.status_code == 404
    assert response.json()["detail"].startswith("Unknown agent")


def test_chat_rejects_cli_only_agent(monkeypatch, tmp_path):
    configure_test_api(monkeypatch, tmp_path)

    with TestClient(app) as client:
        response = client.post("/chat", json={"prompt": "hi", "agent": "coder"})

    assert response.status_code == 403
    assert "CLI-only" in response.json()["detail"]


def test_chat_rejects_empty_prompt(monkeypatch, tmp_path):
    configure_test_api(monkeypatch, tmp_path)

    with TestClient(app) as client:
        response = client.post("/chat", json={"prompt": "", "agent": "vikram"})

    assert response.status_code == 422


def test_chat_uses_stable_conversation_id(monkeypatch, tmp_path):
    configure_test_api(monkeypatch, tmp_path)
    calls = []

    class FakeResult:
        output = "ok"

    class FakeAgent:
        async def run(self, prompt, *, conversation_id):
            calls.append((prompt, conversation_id))
            return FakeResult()

    monkeypatch.setattr(api, "_get_agent", lambda name: FakeAgent())

    with TestClient(app) as client:
        response = client.post("/chat", json={"prompt": "hi", "agent": "vikram"})

    assert response.status_code == 200
    assert response.json() == {"agent": "vikram", "output": "ok"}
    assert calls == [("hi", "chat:vikram")]
