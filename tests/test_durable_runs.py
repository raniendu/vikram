"""Durable agent runs (VIKRAM_DURABLE_AGENT_RUNS) on DBOS.

The unit tests run by default. The end-to-end test boots a real DBOS on a
temporary SQLite database, so it is gated behind VIKRAM_TEST_DBOS=1 (AGENTS.md:
default tests don't boot DBOS workflows).
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from tests.conftest import find_log_event
from vikram.agent import build_agent
from vikram.gateway import ConversationService, ThreadStore
from vikram.settings import VikramSettings
from vikram.spec import load_spec

SPEC_ROOT = Path(__file__).resolve().parent.parent / "spec"


@pytest.fixture
def settings(monkeypatch, tmp_path) -> VikramSettings:
    for name in ("VIKRAM_MODEL", "VIKRAM_MODEL_PROVIDER", "VIKRAM_SPEC_ROOT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("VIKRAM_DURABLE_AGENT_RUNS", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config"))
    return VikramSettings(_env_file=None)


def _capability_names(agent) -> list[str]:
    root = agent.raw_agent.root_capability
    return [type(c).__name__ for c in getattr(root, "capabilities", [root])]


def test_durability_is_on_by_default_and_can_be_turned_off(settings, monkeypatch):
    assert settings.durable_agent_runs is True
    monkeypatch.setenv("VIKRAM_DURABLE_AGENT_RUNS", "false")
    assert VikramSettings(_env_file=None).durable_agent_runs is False


def test_build_agent_alone_is_not_durable(settings):
    """Only threaded conversations opt in (gateway passes the setting)."""
    agent = build_agent(
        spec=load_spec("vikram", SPEC_ROOT), settings=settings, surface="threaded"
    )
    assert "DBOSDurability" not in _capability_names(agent)


def test_durable_flag_adds_dbos_durability(settings, log_events):
    agent = build_agent(
        spec=load_spec("vikram", SPEC_ROOT),
        settings=settings,
        surface="threaded",
        durable=True,
    )
    assert "DBOSDurability" in _capability_names(agent)
    assert find_log_event(log_events, "agent_built")["durable"] is True


def test_threaded_conversations_follow_the_setting(monkeypatch, settings, tmp_path):
    built = []

    def fake_build_agent(**kwargs):
        built.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr("vikram.gateway.build_agent", fake_build_agent)
    for enabled in (False, True):
        service = ConversationService(
            settings=settings.model_copy(update={"durable_agent_runs": enabled}),
            store=ThreadStore(tmp_path / f"t{enabled}.sqlite3"),
        )
        service._get_agent("vikram")
    assert [call["durable"] for call in built] == [False, True]
    assert all(call["surface"] == "threaded" for call in built)


@pytest.mark.skipif(
    os.environ.get("VIKRAM_TEST_DBOS") != "1",
    reason="boots a real DBOS on SQLite; set VIKRAM_TEST_DBOS=1",
)
async def test_completed_steps_are_replayed_not_redone(monkeypatch, settings, tmp_path):
    """Fork a finished run from its last step: earlier model requests come
    from the DBOS journal, so the model is called only for the replayed tail."""
    from dbos import DBOS, SetWorkflowID

    calls: list[int] = []

    def model(messages, info):
        calls.append(len(messages))
        returned = [
            p
            for m in messages
            for p in getattr(m, "parts", [])
            if isinstance(p, ToolReturnPart)
        ]
        if not returned:
            return ModelResponse(parts=[ToolCallPart("clock", {})])
        return ModelResponse(parts=[TextPart(f"done: {returned[0].content}")])

    monkeypatch.setattr(
        "vikram.agent.build_model",
        lambda *a, **k: SimpleNamespace(
            raw=FunctionModel(model), config={"provider": "test", "model": "fn"}
        ),
    )

    DBOS(
        config={
            "name": "vikram-durable-test",
            "system_database_url": f"sqlite:///{tmp_path / 'dbos.sqlite3'}",
            "run_admin_server": False,
        }
    )

    @DBOS.workflow(name="vikram_durable_test_turn")
    async def turn(prompt: str) -> str:
        agent = build_agent(
            spec=load_spec("vikram", SPEC_ROOT),
            settings=settings,
            surface="threaded",
            durable=True,
        )
        agent.raw_agent.tool_plain(lambda: "12:00", name="clock")
        result = await agent.run(prompt, conversation_id="telegram:1")
        return str(result.output)

    DBOS.launch()
    try:
        with SetWorkflowID("turn-1"):
            first = await turn("what time is it?")
        assert first == "done: 12:00"
        assert len(calls) == 2  # tool call request + final answer

        steps = await DBOS.list_workflow_steps_async("turn-1")
        model_steps = [s for s in steps if s["function_name"].endswith("model.request")]
        assert len(model_steps) == 2

        # Resume as if the process died after the first model request.
        handle = await DBOS.fork_workflow_async(
            "turn-1", start_step=model_steps[1]["function_id"]
        )
        assert await handle.get_result() == "done: 12:00"
        assert len(calls) == 3  # only the second request ran again
    finally:
        DBOS.destroy()
