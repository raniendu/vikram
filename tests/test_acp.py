from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import acp
import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import (
    DeferredToolRequests,
    ToolApproved,
    ToolCallPart,
    ToolDenied,
)

from vikram.acp import VikramAcpAgent, _Session


class FakeClient:
    """Captures ACP callbacks the agent makes back to the editor."""

    def __init__(self, permission_outcomes: dict[str, str] | None = None) -> None:
        self.updates: list[Any] = []
        self.permission_requests: list[Any] = []
        # Map tool_call_id -> option_id the fake editor "clicks".
        self._outcomes = permission_outcomes or {}

    async def session_update(self, *, session_id: str, update: Any) -> None:
        self.updates.append((session_id, update))

    async def request_permission(
        self, *, options: Any, session_id: str, tool_call: Any
    ) -> Any:
        self.permission_requests.append(tool_call)
        option_id = self._outcomes.get(tool_call.tool_call_id, "reject")
        return SimpleNamespace(outcome=SimpleNamespace(option_id=option_id))


def _make_agent() -> VikramAcpAgent:
    agent = VikramAcpAgent(settings=SimpleNamespace(spec_root=None), agent_name="coder")
    return agent


async def test_initialize_reports_protocol_and_info():
    agent = _make_agent()
    response = await agent.initialize(protocol_version=acp.PROTOCOL_VERSION)
    assert response.protocol_version == acp.PROTOCOL_VERSION
    assert response.agent_info.name == "vikram-coder"
    # The coder agent is local-only; it should not advertise image/audio input.
    assert response.agent_capabilities.prompt_capabilities.image is False


async def test_new_session_chdirs_and_registers(monkeypatch, tmp_path):
    agent = _make_agent()
    sentinel = object()
    monkeypatch.setattr("vikram.agent.build_agent", lambda **_: sentinel)
    monkeypatch.setattr("vikram.spec.load_spec", lambda *a, **k: object())

    response = await agent.new_session(cwd=str(tmp_path))

    assert response.session_id in agent._sessions
    session = agent._sessions[response.session_id]
    assert session.agent is sentinel
    assert os.path.realpath(os.getcwd()) == os.path.realpath(str(tmp_path))


async def test_prompt_streams_text_and_tool_calls():
    captured: list[str] = []

    async def echo(value: str) -> str:
        """Echo the value back."""
        captured.append(value)
        return f"echoed: {value}"

    test_agent = Agent(TestModel(), tools=[echo])
    agent = _make_agent()
    client = FakeClient()
    agent._client = client
    session = _Session(agent=test_agent, cwd=os.getcwd())

    stop_reason = await agent._run_turn("s1", session, "hello")

    assert stop_reason == "end_turn"
    assert captured, "tool should have been invoked by the model"

    update_types = {type(update).__name__ for _, update in client.updates}
    assert "ToolCallStart" in update_types
    assert "ToolCallProgress" in update_types
    assert "AgentMessageChunk" in update_types
    # History is retained for the next turn.
    assert session.messages


async def test_resolve_permissions_maps_allow_and_deny():
    agent = _make_agent()
    agent._client = FakeClient(permission_outcomes={"c1": "allow", "c2": "reject"})

    requests = DeferredToolRequests(
        approvals=[
            ToolCallPart(
                tool_name="write_file",
                args={"path": "a.py", "content": "x"},
                tool_call_id="c1",
            ),
            ToolCallPart(
                tool_name="run_command",
                args={"argv": ["ls"]},
                tool_call_id="c2",
            ),
        ]
    )

    results = await agent._resolve_permissions("s1", requests)

    assert isinstance(results.approvals["c1"], ToolApproved)
    assert isinstance(results.approvals["c2"], ToolDenied)
    # Both approval prompts were forwarded to the editor with the edit/execute kind.
    forwarded = {tc.tool_call_id: tc.kind for tc in agent._client.permission_requests}
    assert forwarded == {"c1": "edit", "c2": "execute"}


def test_parser_defaults_to_coder():
    from vikram.acp import build_parser

    args = build_parser().parse_args([])
    assert args.agent == "coder"
