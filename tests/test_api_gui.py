from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vikram import api, api_gui
from vikram.api_gui import GUI_ENABLED_ENV, GUI_TOKEN_ENV
from vikram.settings import VikramSettings

TOKEN = "a" * 64
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    """A bare app with only the GUI router, so tests never boot DBOS."""
    monkeypatch.setenv(GUI_TOKEN_ENV, TOKEN)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for name in ("VIKRAM_MODEL", "VIKRAM_MODEL_PROVIDER", "VIKRAM_SPEC_ROOT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(api_gui, "_settings", VikramSettings(_env_file=None))

    app = FastAPI()
    app.include_router(api_gui.router)
    return TestClient(app)


# --- security ----------------------------------------------------------
#
# These four are the reason the phase exists. vikram-api is a deployment
# target that Dockerfile serves on 0.0.0.0, and this router reaches the agent
# store and shell-capable sessions.


def test_router_is_not_mounted_on_the_default_app(monkeypatch):
    """A deployed vikram-api must not expose /v1 at all."""
    monkeypatch.delenv(GUI_ENABLED_ENV, raising=False)

    paths = {route.path for route in api.app.routes}

    assert not any(path.startswith("/v1") for path in paths)
    assert "/healthz" in paths


def test_every_route_rejects_a_missing_token(client, monkeypatch):
    for method, path in [
        ("get", "/v1/agents"),
        ("get", "/v1/tools"),
        ("get", "/v1/providers"),
        ("get", "/v1/config"),
        ("get", "/v1/doctor"),
        ("get", "/v1/schema/agent-draft"),
    ]:
        response = getattr(client, method)(path)
        assert response.status_code == 401, f"{method} {path}"


def test_rejects_a_wrong_token(client):
    response = client.get("/v1/agents", headers={"Authorization": "Bearer wrong"})

    assert response.status_code == 401


def test_rejects_a_non_bearer_scheme(client):
    response = client.get("/v1/agents", headers={"Authorization": f"Basic {TOKEN}"})

    assert response.status_code == 401


def test_refuses_to_serve_the_gui_on_a_non_loopback_host(monkeypatch):
    monkeypatch.setenv(GUI_TOKEN_ENV, TOKEN)

    with pytest.raises(SystemExit, match="Refusing to serve"):
        api.run(["--gui", "--host", "0.0.0.0"])


def test_refuses_to_start_the_gui_without_a_token(monkeypatch):
    monkeypatch.delenv(GUI_TOKEN_ENV, raising=False)
    monkeypatch.delenv(GUI_ENABLED_ENV, raising=False)

    with pytest.raises(SystemExit, match="requires VIKRAM_GUI_TOKEN"):
        api.run(["--gui"])


# --- no credential leaks ----------------------------------------------


def test_providers_never_return_key_material(client, monkeypatch):
    secret = "sk-ant-super-secret-value"
    monkeypatch.setattr(
        api_gui,
        "_settings",
        VikramSettings(_env_file=None, ANTHROPIC_API_KEY=secret),
    )

    body = client.get("/v1/providers", headers=AUTH).text

    assert secret not in body
    rows = {p["id"]: p for p in json.loads(body)["providers"]}
    assert rows["anthropic"]["has_credential"] is True
    assert "api_key" not in rows["anthropic"]


def test_config_reduces_api_keys_to_a_boolean(client, monkeypatch):
    from vikram.config import config_path, merge_write_config

    secret = "sk-openai-secret"
    merge_write_config({"providers": {"openai": {"api_key": secret, "model": "m"}}})
    assert secret in config_path().read_text()

    body = client.get("/v1/config", headers=AUTH).text

    assert secret not in body
    assert json.loads(body)["providers"]["openai"] == {
        "model": "m",
        "base_url": None,
        "has_api_key": True,
    }


def test_mcp_validate_redacts_expanded_secrets(client, monkeypatch):
    """mcp.py keeps url/command/env out of logs; a response is the same exposure."""
    monkeypatch.setenv("SECRET_TOKEN", "tok-abcdef")

    body = client.post(
        "/v1/mcp/validate",
        headers=AUTH,
        json={
            "name": "docs",
            "transport": "http",
            "url": "https://example.test/${SECRET_TOKEN}",
            "headers": {"Authorization": "Bearer ${SECRET_TOKEN}"},
        },
    ).text

    assert "tok-abcdef" not in body
    payload = json.loads(body)
    assert payload["server"]["url"] == "<redacted>"
    assert payload["server"]["header_keys"] == ["Authorization"]


def test_env_check_reports_names_not_values(client, monkeypatch):
    monkeypatch.setenv("PRESENT_VAR", "secret-value")
    monkeypatch.delenv("ABSENT_VAR", raising=False)

    body = client.post(
        "/v1/env/check", headers=AUTH, json={"refs": ["PRESENT_VAR", "ABSENT_VAR"]}
    ).text

    assert "secret-value" not in body
    assert json.loads(body) == {"missing": ["ABSENT_VAR"]}


# --- agents ------------------------------------------------------------


def test_lists_shipped_agents(client):
    payload = client.get("/v1/agents", headers=AUTH).json()

    ids = {a["id"] for a in payload["agents"]}
    assert {"coder", "vikram"} <= ids


def test_get_agent_returns_draft_and_raw_toml(client):
    payload = client.get("/v1/agents/vikram", headers=AUTH).json()

    assert payload["draft"]["name"] == "Vikram"
    assert payload["summary"]["root"] == "builtin"
    assert "[[mcp_servers]]" in payload["source_toml"]


def test_unknown_agent_is_404(client):
    assert client.get("/v1/agents/nope", headers=AUTH).status_code == 404


def test_deleting_a_builtin_is_409(client):
    response = client.delete("/v1/agents/coder", headers=AUTH)

    assert response.status_code == 409
    assert "built-in" in response.json()["detail"]


def test_create_update_and_delete_round_trip(client):
    draft = {
        "name": "Helper",
        "description": "d",
        "system_prompt": "system_prompt.md",
        "tools": ["read_file"],
    }

    created = client.post(
        "/v1/agents",
        headers=AUTH,
        json={"id": "helper", "draft": draft, "system_prompt": "Be helpful."},
    )
    assert created.status_code == 201
    assert created.json()["summary"]["root"] == "user"

    updated = client.put(
        "/v1/agents/helper",
        headers=AUTH,
        json={"draft": {**draft, "name": "Renamed"}},
    )
    assert updated.json()["summary"]["name"] == "Renamed"
    assert updated.json()["system_prompt"] == "Be helpful."

    assert client.delete("/v1/agents/helper", headers=AUTH).status_code == 204
    assert client.get("/v1/agents/helper", headers=AUTH).status_code == 404


def test_create_rejects_a_traversal_id(client):
    response = client.post(
        "/v1/agents",
        headers=AUTH,
        json={
            "id": "../escape",
            "draft": {"name": "x", "description": "d", "system_prompt": "p.md"},
        },
    )

    assert response.status_code == 400


def test_duplicate_creates_a_user_copy(client):
    response = client.post(
        "/v1/agents/coder/duplicate",
        headers=AUTH,
        json={"new_id": "coder-copy", "name": "My Coder"},
    )

    assert response.status_code == 201
    assert response.json()["summary"]["root"] == "user"
    assert response.json()["summary"]["name"] == "My Coder"


def test_editing_a_builtin_leaves_the_shipped_file_alone(client):
    settings = VikramSettings(_env_file=None)
    shipped = settings.spec_root / "coder" / "agent.toml"
    before = shipped.read_text()

    client.put(
        "/v1/agents/coder",
        headers=AUTH,
        json={"draft": {"name": "Mine", "description": "d", "system_prompt": "p.md"}},
    )

    assert shipped.read_text() == before


# --- validation and preview -------------------------------------------


def test_validate_reports_an_unknown_tool(client):
    payload = client.post(
        "/v1/agents/validate",
        headers=AUTH,
        json={
            "draft": {
                "name": "x",
                "description": "d",
                "system_prompt": "p.md",
                "tools": ["not_a_tool"],
            }
        },
    ).json()

    assert payload["ok"] is False
    assert payload["issues"][0]["field"] == "tools"
    assert "not_a_tool" in payload["issues"][0]["message"]


def test_prompt_preview_returns_the_assembled_prompt(client):
    payload = client.get("/v1/agents/vikram/prompt-preview", headers=AUTH).json()

    assert payload["length"] > 500
    assert "## Available skills" in payload["system_prompt"]
    assert "web_search" in payload["tool_names"]


def test_prompt_preview_does_not_swap_the_global_command_policy(client):
    """Preview runs while other agents may be mid-run_command."""
    from vikram import tools

    client.get("/v1/agents/coder/prompt-preview", headers=AUTH)
    sentinel = object()
    tools._ACTIVE_POLICY = sentinel

    client.get("/v1/agents/coder/prompt-preview", headers=AUTH)

    assert tools._ACTIVE_POLICY is sentinel


# --- registries --------------------------------------------------------


def test_tool_catalog_flags_approval_gated_tools(client):
    rows = {t["name"]: t for t in client.get("/v1/tools", headers=AUTH).json()["tools"]}

    assert rows["write_file"]["requires_approval"] is True
    assert rows["read_file"]["requires_approval"] is False
    assert "delegate_to_agent" in rows
    assert rows["read_file"]["description"]


def test_schemas_come_from_the_runtime_models(client):
    draft = client.get("/v1/schema/agent-draft", headers=AUTH).json()
    mcp = client.get("/v1/schema/mcp-server", headers=AUTH).json()

    assert "tools" in draft["properties"]
    assert "agent_dir" not in draft["properties"]
    assert "transport" in mcp["properties"]


def test_skills_are_split_by_origin(client):
    payload = client.get("/v1/skills?agent_id=coder", headers=AUTH).json()

    assert {s["name"] for s in payload["agent"]} == {"conventional-commits"}
    assert {s["name"] for s in payload["shared"]} == {"web-research"}


def test_doctor_returns_diagnostics(client):
    payload = client.get("/v1/doctor", headers=AUTH).json()

    names = {d["name"] for d in payload["diagnostics"]}
    assert {"Python", "Spec root", "Agent spec"} <= names


# --- config writes -----------------------------------------------------


def test_put_provider_then_clear_an_agent_override(client):
    client.put(
        "/v1/config/providers/ollama",
        headers=AUTH,
        json={"model": "gemma4:26b-a4b-it-qat"},
    )
    client.put(
        "/v1/config/agents/coder",
        headers=AUTH,
        json={"provider": "ollama", "model": "override-model"},
    )
    assert client.get("/v1/config", headers=AUTH).json()["agents"]["coder"] == {
        "provider": "ollama",
        "model": "override-model",
    }

    client.delete("/v1/config/agents/coder", headers=AUTH)

    assert "coder" not in client.get("/v1/config", headers=AUTH).json()["agents"]


def test_unknown_provider_is_404(client):
    response = client.put(
        "/v1/config/providers/not-a-provider", headers=AUTH, json={"model": "m"}
    )

    assert response.status_code == 404


def test_config_surfaces_a_top_level_model_so_the_ui_can_warn(client):
    """A top-level model= beats every spec pin; the GUI must never write one."""
    from vikram.config import config_path

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('config_version = 2\nmodel = "sneaky"\n')

    assert client.get("/v1/config", headers=AUTH).json()["top_level_model"] == "sneaky"


def test_gui_mode_keeps_logs_off_stdout(monkeypatch, tmp_path, capsys):
    """stdout is the desktop shell's handshake channel.

    structlog defaults to stdout, so without an explicit stream a single log
    line corrupts the port announcement the shell parses.
    """
    import socket
    import sys

    monkeypatch.setenv(GUI_TOKEN_ENV, TOKEN)
    served: dict = {}

    class _FakeServer:
        def __init__(self, config):
            served["config"] = config

        def run(self, sockets=None):
            served["sockets"] = sockets

    monkeypatch.setattr(api, "mount_gui", lambda *a, **k: None)
    monkeypatch.setattr("uvicorn.Server", _FakeServer)
    monkeypatch.setattr("uvicorn.Config", lambda *a, **k: {"args": a, "kwargs": k})

    api.run(["--gui", "--port", "0"])

    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 1, f"stdout must carry only the handshake, got: {lines}"

    handshake = json.loads(lines[0])
    assert handshake["vikram_api_ready"] is True
    assert handshake["gui"] is True
    assert isinstance(handshake["port"], int) and handshake["port"] > 0
    assert api._log_stream is sys.stderr

    for sock in served.get("sockets") or []:
        sock.close()


def test_handshake_reports_the_bound_port_not_the_requested_one(monkeypatch, capsys):
    """--port 0 must announce the real port, with no bind race."""
    monkeypatch.setenv(GUI_TOKEN_ENV, TOKEN)
    served: dict = {}

    class _FakeServer:
        def __init__(self, config):
            pass

        def run(self, sockets=None):
            served["sockets"] = sockets

    monkeypatch.setattr(api, "mount_gui", lambda *a, **k: None)
    monkeypatch.setattr("uvicorn.Server", _FakeServer)
    monkeypatch.setattr("uvicorn.Config", lambda *a, **k: None)

    api.run(["--gui", "--port", "0"])

    handshake = json.loads(capsys.readouterr().out.splitlines()[0])
    sockets = served["sockets"]
    assert sockets[0].getsockname()[1] == handshake["port"]
    for sock in sockets:
        sock.close()
