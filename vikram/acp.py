"""Agent Client Protocol (ACP) front-end for the local Coder agent.

This exposes Vikram's CLI-only ``coder`` agent over the `Agent Client Protocol
<https://agentclientprotocol.com>`_ so editors such as Zed or Neovim can drive
it. It is a thin adapter: every turn is still executed by ``build_agent`` and
Strands streaming (same engine as ``vikram/cli.py``); only the I/O surface changes
from a Rich REPL to JSON-RPC ``session/update`` notifications over stdio.

Run it directly (the editor normally launches this for you)::

    vikram-acp --agent coder
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import acp

from vikram.streaming import tool_result_from_event as _tool_result_from_event
from vikram.streaming import tool_results_from_event as _tool_results_from_event
from vikram.streaming import tool_use_from_event as _tool_use_from_event

if TYPE_CHECKING:
    from vikram.agent import VikramAgent
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
    if isinstance(part, dict):
        return part.get("input") or part.get("args")
    try:
        return part.args_as_dict()
    except Exception:
        return getattr(part, "args", None)


def _render_call(part: Any) -> str:
    args = _safe_args(part)
    name = (
        part.get("name") if isinstance(part, dict) else getattr(part, "tool_name", None)
    ) or "?"
    if isinstance(args, dict) and args:
        rendered = ", ".join(f"{k}={v!r}" for k, v in args.items())
        return f"{name}({rendered})"
    return f"{name}()"


def _stringify_result(part: Any) -> str:
    if isinstance(part, dict):
        rendered: list[str] = []
        for item in part.get("content") or []:
            if isinstance(item, dict):
                if "text" in item:
                    rendered.append(str(item["text"]))
                elif "json" in item:
                    rendered.append(json.dumps(item["json"], default=str))
            else:
                rendered.append(str(item))
        text = "\n".join(rendered)
        return (
            text[: _MAX_TOOL_BODY_CHARS - 1] + "…"
            if len(text) > _MAX_TOOL_BODY_CHARS
            else text
        )
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
    agent: "VikramAgent"
    cwd: str
    messages: list[Any] = field(default_factory=list)
    task: "asyncio.Task[Any] | None" = None


class VikramAcpAgent(acp.Agent):
    """ACP agent that wraps the local ``coder`` Strands agent."""

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
        session_id = uuid.uuid4().hex
        spec = load_spec(self._agent_name, self._settings.spec_root)

        async def approval_ask(prompt: str) -> str:
            return await self._request_permission_from_hitl_prompt(session_id, prompt)

        agent = build_agent(
            spec=spec,
            settings=self._settings,
            surface="acp",
            approval_ask=approval_ask,
        )
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
        async for event in session.agent.stream_events(
            prompt,
            message_history=session.messages,
            conversation_id=f"acp:{session_id}",
        ):
            if isinstance(event, dict) and "vikram_result" in event:
                session.messages = list(event["vikram_result"].all_messages())
                continue
            await self._stream_event(session_id, event)
        return "end_turn"

    async def _stream_event(self, session_id: str, event: Any) -> None:
        if not isinstance(event, dict):
            return
        reasoning = event.get("reasoningText")
        if reasoning:
            await self._notify(
                session_id, acp.update_agent_thought_text(str(reasoning))
            )
        data = event.get("data")
        if data:
            await self._notify(session_id, acp.update_agent_message_text(str(data)))

        tool_use = _tool_use_from_event(event)
        if tool_use is not None:
            tool_call_id = str(
                tool_use.get("toolUseId") or tool_use.get("id") or uuid.uuid4().hex
            )
            name = str(tool_use.get("name") or "?")
            await self._notify(
                session_id,
                acp.start_tool_call(
                    tool_call_id=tool_call_id,
                    title=_render_call(tool_use),
                    kind=_tool_kind(name),
                    status="in_progress",
                    raw_input=_safe_args(tool_use),
                ),
            )

        for tool_result in _tool_results_from_event(event):
            body = _stringify_result(tool_result)
            failed = str(tool_result.get("status") or "success") == "error"
            await self._notify(
                session_id,
                acp.update_tool_call(
                    tool_call_id=str(tool_result.get("toolUseId") or ""),
                    status="failed" if failed else "completed",
                    content=(
                        [acp.tool_content(acp.text_block(body))] if body else None
                    ),
                ),
            )

    async def _request_permission_from_hitl_prompt(
        self, session_id: str, prompt: str
    ) -> str:
        tool_name, raw_input = _parse_hitl_prompt(prompt)
        options = [
            acp.schema.PermissionOption(
                option_id="allow", name="Allow", kind="allow_once"
            ),
            acp.schema.PermissionOption(
                option_id="reject", name="Reject", kind="reject_once"
            ),
        ]
        tool_call = acp.schema.ToolCallUpdate(
            tool_call_id=uuid.uuid4().hex,
            title=_render_call({"name": tool_name, "input": raw_input}),
            kind=_tool_kind(tool_name),
            status="pending",
            raw_input=raw_input,
        )
        assert self._client is not None
        response = await self._client.request_permission(
            options=options, session_id=session_id, tool_call=tool_call
        )
        return (
            "yes" if getattr(response.outcome, "option_id", None) == "allow" else "no"
        )

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


_HITL_PROMPT = re.compile(
    r'^Tool "(?P<name>[^"]+)" requires human approval\. Input: (?P<input>.*)$'
)


def _parse_hitl_prompt(prompt: str) -> tuple[str, Any]:
    match = _HITL_PROMPT.match(prompt.strip())
    if match is None:
        return "?", {"prompt": prompt}
    raw_input = match.group("input")
    try:
        parsed_input = json.loads(raw_input)
    except json.JSONDecodeError:
        parsed_input = {"input": raw_input}
    return match.group("name"), parsed_input


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
