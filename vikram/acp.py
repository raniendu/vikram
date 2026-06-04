"""Agent Client Protocol (ACP) front-end for the local Coder agent.

This exposes Vikram's CLI-only ``coder`` agent over the `Agent Client Protocol
<https://agentclientprotocol.com>`_ so editors such as Zed or Neovim can drive
it. It is a thin adapter: every turn is still executed by ``build_agent`` +
``agent.iter`` (same engine as ``vikram/cli.py``); only the I/O surface changes
from a Rich REPL to JSON-RPC ``session/update`` notifications over stdio.

Run it directly (the editor normally launches this for you)::

    vikram-acp --agent coder
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import acp

if TYPE_CHECKING:
    from pydantic_ai import Agent
    from pydantic_ai.messages import ModelMessage

    from vikram.settings import VikramSettings

# Tools the coder agent marks ``requires_approval=True`` map to ACP permission
# prompts. Keep the kinds aligned with vikram.tools.TOOL_REGISTRY.
_TOOL_KINDS: dict[str, str] = {
    "read_file": "read",
    "glob": "search",
    "grep": "search",
    "inspect_command": "execute",
    "run_command": "execute",
    "write_file": "edit",
    "edit_file": "edit",
}

_MAX_TOOL_BODY_CHARS = 4_000


def _tool_kind(tool_name: str | None) -> str:
    return _TOOL_KINDS.get(tool_name or "", "other")


def _safe_args(part: Any) -> Any:
    try:
        return part.args_as_dict()
    except Exception:
        return getattr(part, "args", None)


def _render_call(part: Any) -> str:
    args = _safe_args(part)
    name = getattr(part, "tool_name", None) or "?"
    if isinstance(args, dict) and args:
        rendered = ", ".join(f"{k}={v!r}" for k, v in args.items())
        return f"{name}({rendered})"
    return f"{name}()"


def _stringify_result(part: Any) -> str:
    content = getattr(part, "content", "")
    if isinstance(content, str):
        text = content
    else:
        try:
            text = "\n".join(str(item) for item in part.content_items(mode="str"))
        except Exception:
            text = str(content)
    if len(text) > _MAX_TOOL_BODY_CHARS:
        text = text[: _MAX_TOOL_BODY_CHARS - 1] + "…"
    return text


@dataclass
class _Session:
    agent: "Agent"
    cwd: str
    messages: list["ModelMessage"] = field(default_factory=list)
    task: "asyncio.Task[Any] | None" = None


class VikramAcpAgent(acp.Agent):
    """ACP agent that wraps the local ``coder`` pydantic-ai agent."""

    def __init__(
        self, *, settings: "VikramSettings", agent_name: str = "coder"
    ) -> None:
        self._settings = settings
        self._agent_name = agent_name
        self._client: acp.Client | None = None
        self._sessions: dict[str, _Session] = {}

    # ACP plumbing ---------------------------------------------------------
    def on_connect(self, conn: acp.Client) -> None:
        self._client = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any | None = None,
        client_info: Any | None = None,
        **_: Any,
    ) -> acp.InitializeResponse:
        from vikram import __version__

        return acp.InitializeResponse(
            protocol_version=acp.PROTOCOL_VERSION,
            agent_info=acp.schema.Implementation(
                name="vikram-coder", version=__version__
            ),
            agent_capabilities=acp.schema.AgentCapabilities(
                load_session=False,
                prompt_capabilities=acp.schema.PromptCapabilities(
                    image=False, audio=False, embedded_context=True
                ),
            ),
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: Any | None = None,
        **_: Any,
    ) -> acp.NewSessionResponse:
        from vikram.agent import build_agent
        from vikram.spec import load_spec

        # The coder tools resolve paths against the process cwd (see
        # vikram.tools._workspace_root). Editors launch one agent process per
        # workspace, so honoring the session cwd here keeps tool access scoped
        # to the project the editor opened.
        if cwd:
            os.chdir(cwd)
        spec = load_spec(self._agent_name, self._settings.spec_root)
        agent = build_agent(spec=spec, settings=self._settings)
        session_id = uuid.uuid4().hex
        self._sessions[session_id] = _Session(agent=agent, cwd=cwd or os.getcwd())
        return acp.NewSessionResponse(session_id=session_id)

    async def prompt(
        self,
        prompt: list[Any],
        session_id: str,
        message_id: str | None = None,
        **_: Any,
    ) -> acp.PromptResponse:
        session = self._sessions.get(session_id)
        if session is None:
            raise acp.RequestError.invalid_params(f"unknown session {session_id!r}")
        if session.cwd:
            os.chdir(session.cwd)

        text = self._prompt_text(prompt)
        task = asyncio.ensure_future(self._run_turn(session_id, session, text))
        session.task = task
        try:
            stop_reason = await task
        except asyncio.CancelledError:
            stop_reason = "cancelled"
        finally:
            session.task = None
        return acp.PromptResponse(stop_reason=stop_reason)

    async def cancel(self, session_id: str, **_: Any) -> None:
        session = self._sessions.get(session_id)
        if session and session.task is not None and not session.task.done():
            session.task.cancel()

    # Turn execution -------------------------------------------------------
    async def _run_turn(self, session_id: str, session: _Session, prompt: str) -> str:
        from pydantic_ai import Agent as _Agent
        from pydantic_ai.capabilities import HandleDeferredToolCalls

        async def approval_handler(ctx: Any, requests: Any) -> Any:
            return await self._resolve_permissions(session_id, requests)

        capabilities = [HandleDeferredToolCalls(handler=approval_handler)]
        async with session.agent.iter(
            prompt, message_history=session.messages, capabilities=capabilities
        ) as run:
            async for node in run:
                if _Agent.is_model_request_node(node):
                    async with node.stream(run.ctx) as stream:
                        await self._stream_model_request(session_id, stream)
                elif _Agent.is_call_tools_node(node):
                    async with node.stream(run.ctx) as stream:
                        await self._stream_tool_calls(session_id, stream)
            assert run.result is not None
            session.messages = list(run.result.all_messages())
        return "end_turn"

    async def _stream_model_request(self, session_id: str, stream: Any) -> None:
        from pydantic_ai.messages import (
            PartDeltaEvent,
            PartStartEvent,
            TextPartDelta,
            ThinkingPartDelta,
        )

        async for event in stream:
            if isinstance(event, PartStartEvent):
                initial = getattr(event.part, "content", "") or ""
                if not isinstance(initial, str) or not initial:
                    continue
                if event.part.part_kind == "text":
                    await self._notify(
                        session_id, acp.update_agent_message_text(initial)
                    )
                elif event.part.part_kind == "thinking":
                    await self._notify(
                        session_id, acp.update_agent_thought_text(initial)
                    )
            elif isinstance(event, PartDeltaEvent):
                delta = event.delta
                if isinstance(delta, TextPartDelta) and delta.content_delta:
                    await self._notify(
                        session_id, acp.update_agent_message_text(delta.content_delta)
                    )
                elif isinstance(delta, ThinkingPartDelta) and delta.content_delta:
                    await self._notify(
                        session_id, acp.update_agent_thought_text(delta.content_delta)
                    )

    async def _stream_tool_calls(self, session_id: str, stream: Any) -> None:
        from pydantic_ai.messages import FunctionToolCallEvent, FunctionToolResultEvent

        async for event in stream:
            if isinstance(event, FunctionToolCallEvent):
                part = event.part
                await self._notify(
                    session_id,
                    acp.start_tool_call(
                        tool_call_id=event.tool_call_id,
                        title=_render_call(part),
                        kind=_tool_kind(part.tool_name),
                        status="in_progress",
                        raw_input=_safe_args(part),
                    ),
                )
            elif isinstance(event, FunctionToolResultEvent):
                part = event.part
                failed = part.part_kind == "retry-prompt"
                body = _stringify_result(part)
                await self._notify(
                    session_id,
                    acp.update_tool_call(
                        tool_call_id=event.tool_call_id,
                        status="failed" if failed else "completed",
                        content=(
                            [acp.tool_content(acp.text_block(body))] if body else None
                        ),
                    ),
                )

    async def _resolve_permissions(self, session_id: str, requests: Any) -> Any:
        from pydantic_ai.exceptions import ModelRetry
        from pydantic_ai.tools import ToolApproved, ToolDenied

        approvals: dict[str, ToolApproved | ToolDenied] = {}
        calls: dict[str, ModelRetry] = {}

        options = [
            acp.schema.PermissionOption(
                option_id="allow", name="Allow", kind="allow_once"
            ),
            acp.schema.PermissionOption(
                option_id="reject", name="Reject", kind="reject_once"
            ),
        ]
        for call in requests.approvals:
            tool_call = acp.schema.ToolCallUpdate(
                tool_call_id=call.tool_call_id,
                title=_render_call(call),
                kind=_tool_kind(call.tool_name),
                status="pending",
                raw_input=_safe_args(call),
            )
            assert self._client is not None
            response = await self._client.request_permission(
                options=options, session_id=session_id, tool_call=tool_call
            )
            if getattr(response.outcome, "option_id", None) == "allow":
                approvals[call.tool_call_id] = ToolApproved()
            else:
                approvals[call.tool_call_id] = ToolDenied("User denied this tool call.")

        for call in requests.calls:
            calls[call.tool_call_id] = ModelRetry(
                "External deferred tool calls are not supported by the Vikram ACP agent."
            )

        return requests.build_results(approvals=approvals, calls=calls)

    # Helpers --------------------------------------------------------------
    async def _notify(self, session_id: str, update: Any) -> None:
        assert self._client is not None
        await self._client.session_update(session_id=session_id, update=update)

    @staticmethod
    def _prompt_text(prompt: list[Any]) -> str:
        parts: list[str] = []
        for block in prompt:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vikram-acp")
    parser.add_argument(
        "--agent",
        default="coder",
        help="Agent name to load from spec/ (default: coder)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    from vikram.settings import VikramSettings

    args = build_parser().parse_args(list(argv) if argv is not None else sys.argv[1:])
    settings = VikramSettings()
    agent = VikramAcpAgent(settings=settings, agent_name=args.agent)
    asyncio.run(acp.run_agent(agent))


if __name__ == "__main__":
    main()
