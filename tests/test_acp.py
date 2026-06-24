from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import acp
import pytest

from vikram.acp import VikramAcpAgent, _Session, _tool_result_from_event


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
    class FakeResult:
        def all_messages(self):
            return [{"role": "assistant", "content": "done"}]

    class FakeStrandsAgent:
        async def stream_events(self, prompt, *, message_history, conversation_id):
            assert prompt == "hello"
            assert message_history == []
            assert conversation_id == "acp:s1"
            yield {"data": "thinking text"}
            yield {
                "event": {
                    "contentBlockStart": {
                        "start": {
                            "toolUse": {
                                "toolUseId": "call-1",
                                "name": "run_command",
                                "input": {"command": "git status --short"},
                            }
                        }
                    }
                }
            }
            yield {
                "tool_result": {
                    "toolUseId": "call-1",
                    "status": "success",
                    "content": [{"text": "clean"}],
                }
            }
            yield {"vikram_result": FakeResult()}

    agent = _make_agent()
    client = FakeClient()
    agent._client = client
    session = _Session(agent=FakeStrandsAgent(), cwd=os.getcwd())

    stop_reason = await agent._run_turn("s1", session, "hello")

    assert stop_reason == "end_turn"

    update_types = {type(update).__name__ for _, update in client.updates}
    assert "ToolCallStart" in update_types
    assert "ToolCallProgress" in update_types
    assert "AgentMessageChunk" in update_types
    # History is retained for the next turn.
    assert session.messages


def test_tool_result_extracts_strands_message_event():
    result = _tool_result_from_event(
        {
            "message": {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "call-1",
                            "status": "success",
                            "content": [{"text": "done"}],
                        }
                    }
                ],
            }
        }
    )

    assert result == {
        "toolUseId": "call-1",
        "status": "success",
        "content": [{"text": "done"}],
    }


async def test_stream_event_reports_all_concurrent_tool_results():
    agent = _make_agent()
    client = FakeClient()
    agent._client = client

    await agent._stream_event(
        "s1",
        {
            "message": {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "call-1",
                            "status": "success",
                            "content": [{"text": "first"}],
                        }
                    },
                    {
                        "toolResult": {
                            "toolUseId": "call-2",
                            "status": "error",
                            "content": [{"text": "second"}],
                        }
                    },
                ],
            }
        },
    )

    progress = [
        update
        for _, update in client.updates
        if type(update).__name__ == "ToolCallProgress"
    ]
    assert [(update.tool_call_id, update.status) for update in progress] == [
        ("call-1", "completed"),
        ("call-2", "failed"),
    ]


async def test_hitl_prompt_permission_maps_allow_and_deny():
    agent = _make_agent()
    agent._client = FakeClient(permission_outcomes={"c1": "allow", "c2": "reject"})
    ids = iter(["c1", "c2"])

    import vikram.acp as acp_module

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        acp_module.uuid, "uuid4", lambda: SimpleNamespace(hex=next(ids))
    )
    try:
        allowed = await agent._request_permission_from_hitl_prompt(
            "s1",
            'Tool "write_file" requires human approval. Input: {"path": "a.py", "content": "x"}',
        )
        denied = await agent._request_permission_from_hitl_prompt(
            "s1",
            'Tool "run_command" requires human approval. Input: {"command": "ls"}',
        )
    finally:
        monkeypatch.undo()

    assert allowed == "yes"
    assert denied == "no"
    # Both approval prompts were forwarded to the editor with the edit/execute kind.
    forwarded = {tc.tool_call_id: tc.kind for tc in agent._client.permission_requests}
    assert forwarded == {"c1": "edit", "c2": "execute"}


def test_parser_defaults_to_coder():
    from vikram.acp import build_parser

    args = build_parser().parse_args([])
    assert args.agent == "coder"
