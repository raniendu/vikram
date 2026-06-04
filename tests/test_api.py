from fastapi.testclient import TestClient

from vikram import api
from vikram.api import app


def test_healthz_returns_ok():
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_chat_unknown_agent_returns_404():
    with TestClient(app) as client:
        response = client.post("/chat", json={"prompt": "hi", "agent": "missing"})

    assert response.status_code == 404
    assert response.json()["detail"].startswith("Unknown agent")


def test_chat_rejects_cli_only_agent():
    with TestClient(app) as client:
        response = client.post("/chat", json={"prompt": "hi", "agent": "coder"})

    assert response.status_code == 403
    assert "CLI-only" in response.json()["detail"]


def test_chat_rejects_empty_prompt():
    with TestClient(app) as client:
        response = client.post("/chat", json={"prompt": "", "agent": "vikram"})

    assert response.status_code == 422


def test_chat_uses_stable_conversation_id(monkeypatch):
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
