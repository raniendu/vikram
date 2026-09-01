from fastapi.testclient import TestClient

from tests.conftest import captured_json_logs
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
    assert "local-only" in response.json()["detail"]


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


def test_request_id_is_generated_and_echoed(monkeypatch, tmp_path):
    configure_test_api(monkeypatch, tmp_path)

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.headers["x-request-id"]


def test_caller_supplied_request_id_is_preserved(monkeypatch, tmp_path):
    """A caller's correlation id must survive so traces line up across systems."""
    configure_test_api(monkeypatch, tmp_path)

    with TestClient(app) as client:
        response = client.get("/healthz", headers={"x-request-id": "abc-123"})

    assert response.headers["x-request-id"] == "abc-123"


def test_request_id_is_bound_to_every_log_line_in_the_request(monkeypatch, tmp_path):
    configure_test_api(monkeypatch, tmp_path)

    with TestClient(app) as client:
        # Capture inside the client context: startup runs configure_logging,
        # which would otherwise replace the capturing handler.
        with captured_json_logs() as logs:
            client.post("/chat", json={"prompt": "hi", "agent": "missing"})

    rejected = [entry for entry in logs if entry["event"] == "chat_rejected"]
    assert rejected, [entry["event"] for entry in logs]

    finished = [entry for entry in logs if entry["event"] == "http_request_finished"]
    assert finished[-1]["status_code"] == 404
    assert finished[-1]["path"] == "/chat"
    assert finished[-1]["duration_ms"] >= 0

    # The handler's own log line and the request summary must share one id —
    # that correlation is the whole point of the middleware.
    assert rejected[0]["request_id"]
    assert rejected[0]["request_id"] == finished[-1]["request_id"]


def test_request_id_does_not_leak_between_requests(monkeypatch, tmp_path):
    configure_test_api(monkeypatch, tmp_path)

    with TestClient(app) as client:
        with captured_json_logs() as logs:
            client.get("/healthz")
            client.get("/healthz")

    finished = [entry for entry in logs if entry["event"] == "http_request_finished"]
    assert len(finished) == 2
    assert finished[0]["request_id"] != finished[1]["request_id"]


def test_readyz_reports_healthy_dependencies(monkeypatch, tmp_path):
    configure_test_api(monkeypatch, tmp_path)

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == {
        "model_config": "ok",
        "thread_store": "ok",
        "default_agent": "ok",
    }
    assert body["default_agent"] == "vikram"


def test_readyz_reports_503_when_the_model_is_unconfigured(monkeypatch, tmp_path):
    """An unconfigured deploy must fail readiness instead of accepting traffic."""
    configure_test_api(monkeypatch, tmp_path)

    with TestClient(app) as client:
        # Swap in unconfigured settings after startup: the lifespan eagerly
        # builds the default agent, so a process that boots this way never
        # reaches readiness at all. XDG_CONFIG_HOME points at an empty dir so
        # the developer's own ~/.config/vikram/config.toml cannot leak in.
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config"))
        monkeypatch.setattr(
            api,
            "_settings",
            VikramSettings(
                _env_file=None,
                VIKRAM_DB_PATH=tmp_path / "vikram.sqlite3",
                DBOS_SYSTEM_DATABASE_URL=f"sqlite:///{tmp_path / 'dbos.sqlite3'}",
            ),
        )
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["checks"]["model_config"] == "unconfigured"


def test_readyz_reports_503_when_a_dependency_raises(monkeypatch, tmp_path):
    configure_test_api(monkeypatch, tmp_path)

    def boom() -> None:
        raise RuntimeError("store is gone")

    monkeypatch.setattr(api, "_get_store", boom)

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["checks"]["thread_store"] == "error: RuntimeError"
