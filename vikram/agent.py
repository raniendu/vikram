from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from strands import Agent, tool
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent
from strands.vended_interventions.hitl import HumanInTheLoop

from vikram.context import agent_identity, current_datetime
from vikram.delegation import (
    DELEGATE_TOOL_NAME,
    make_delegate_to_agent_tool,
    subagent_instructions,
)
from vikram.hooks import HookBlockedError, HookSet, build_hooks, run_hooks
from vikram.mcp import VikramMCPClient, build_mcp_servers
from vikram.settings import VikramModel, VikramSettings, build_model
from vikram.skills import discover_skills, make_load_skill_tool, skills_instructions
from vikram.spec import AgentSpec, load_spec
from vikram.tools import TOOL_REGISTRY, ToolEntry, VikramTool, set_command_policy


class AgentToolError(RuntimeError):
    """Raised when an agent spec references tools unavailable to this package."""


@dataclass
class VikramRunResult:
    output: str
    messages: list[Any]
    input_tokens: int = 0

    def all_messages(self) -> list[Any]:
        return self.messages

    def all_messages_json(self) -> bytes:
        return json.dumps(self.messages, default=str).encode("utf-8")

    def usage(self) -> Any:
        return SimpleNamespace(input_tokens=self.input_tokens)


class VikramAgent:
    """Small compatibility wrapper around a Strands agent."""

    runtime = "strands"

    def __init__(
        self,
        *,
        raw_agent: Agent,
        agent_kwargs: dict[str, Any],
        name: str,
        description: str,
        model: VikramModel,
        system_prompt: str,
        tools: list[VikramTool],
        mcp_clients: list[VikramMCPClient],
        hooks: HookSet,
    ) -> None:
        self.raw_agent = raw_agent
        self._agent_kwargs = agent_kwargs
        self.name = name
        self.description = description
        self.model = model.raw
        self.model_config = model.config
        self.system_prompt = system_prompt
        self.tools = tools
        self.tool_names = [entry.name for entry in tools]
        self.approval_tool_names = [
            entry.name for entry in tools if entry.requires_approval
        ]
        self.mcp_clients = mcp_clients
        self._hookset = hooks

    async def run(
        self,
        user_prompt: str,
        *,
        message_history: list[Any] | None = None,
        conversation_id: str | None = None,
        **kwargs: Any,
    ) -> VikramRunResult:
        prompt = await self._apply_user_prompt_hooks(user_prompt)
        agent = self._agent_for_run()
        if message_history is not None:
            agent.messages = list(message_history)
        else:
            agent.messages = []
        result = await agent.invoke_async(
            prompt,
            invocation_state={"conversation_id": conversation_id, **kwargs},
        )
        output = str(result)
        if self._hookset.stop:
            await run_hooks(
                self._hookset.stop,
                {
                    "event": "Stop",
                    "agent": self.name,
                    "output": output,
                    "cwd": _cwd(),
                },
            )
        messages = list(getattr(agent, "messages", []) or [])
        input_tokens = int(getattr(result, "context_size", None) or 0)
        return VikramRunResult(
            output=output, messages=messages, input_tokens=input_tokens
        )

    def run_sync(self, user_prompt: str, **kwargs: Any) -> VikramRunResult:
        return asyncio.run(self.run(user_prompt, **kwargs))

    async def stream_events(
        self,
        user_prompt: str,
        *,
        message_history: list[Any] | None = None,
        conversation_id: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        prompt = await self._apply_user_prompt_hooks(user_prompt)
        agent = self._agent_for_run()
        if message_history is not None:
            agent.messages = list(message_history)
        else:
            agent.messages = []
        chunks: list[str] = []
        raw_result: Any | None = None
        async for event in agent.stream_async(
            prompt,
            invocation_state={"conversation_id": conversation_id, **kwargs},
        ):
            if isinstance(event, dict) and "data" in event:
                chunks.append(str(event["data"]))
            if isinstance(event, dict) and "result" in event:
                raw_result = event["result"]
            yield event
        output = str(raw_result) if raw_result is not None else "".join(chunks)
        result = VikramRunResult(
            output=output,
            messages=list(getattr(agent, "messages", []) or []),
            input_tokens=int(getattr(raw_result, "context_size", None) or 0),
        )
        if self._hookset.stop:
            await run_hooks(
                self._hookset.stop,
                {
                    "event": "Stop",
                    "agent": self.name,
                    "output": output,
                    "cwd": _cwd(),
                },
            )
        yield {"vikram_result": result}

    async def _apply_user_prompt_hooks(self, user_prompt: str) -> str:
        if not self._hookset.user_prompt_submit:
            return user_prompt
        decision = await run_hooks(
            self._hookset.user_prompt_submit,
            {
                "event": "UserPromptSubmit",
                "agent": self.name,
                "prompt": user_prompt,
                "cwd": _cwd(),
            },
        )
        if decision.blocked:
            raise HookBlockedError(decision.reason or "A hook blocked this prompt.")
        if decision.context:
            return f"{decision.context}\n\n{user_prompt}"
        return user_prompt

    def _agent_for_run(self) -> Agent:
        return Agent(
            **{
                **self._agent_kwargs,
                "tools": [
                    *self._agent_kwargs["tools"],
                    *(client.raw for client in self.mcp_clients),
                ],
            }
        )


def _cwd() -> str:
    import os

    return os.getcwd()


def _resolve_tools(
    spec: AgentSpec,
    *,
    settings: VikramSettings,
    surface: str,
    enable_delegation: bool,
) -> list[ToolEntry]:
    missing = [
        name
        for name in spec.tools
        if name != DELEGATE_TOOL_NAME and name not in TOOL_REGISTRY
    ]
    if missing:
        missing_list = ", ".join(missing)
        raise AgentToolError(
            f"Agent {spec.name} references unknown tool(s): {missing_list}. "
            "The installed Vikram package may be stale relative to the agent "
            "specs; run `vikram update` or reinstall the vikram uv tool."
        )

    tools: list[ToolEntry] = []
    for name in spec.tools:
        if name == DELEGATE_TOOL_NAME:
            if enable_delegation:
                tools.append(
                    make_delegate_to_agent_tool(
                        settings=settings,
                        orchestrator_name=spec.agent_dir.name,
                        surface=surface,
                        requires_approval=surface == "cli",
                    )
                )
            continue
        tools.append(TOOL_REGISTRY[name])
    return tools


def build_agent(
    spec: AgentSpec | None = None,
    settings: VikramSettings | None = None,
    *,
    surface: str = "cli",
    enable_delegation: bool = True,
    approve_all: bool = False,
    approval_ask: Any | None = None,
) -> VikramAgent:
    settings = settings or VikramSettings()
    spec = spec or load_spec(settings.default_agent, settings.spec_root)
    settings = _settings_with_spec_model(settings, spec)
    tools = _resolve_tools(
        spec,
        settings=settings,
        surface=surface,
        enable_delegation=enable_delegation,
    )
    set_command_policy(spec.load_command_policy())

    skills = discover_skills(spec)
    instructions: list[Any] = [spec.instructions, agent_identity(spec.name)]
    skills_block = skills_instructions(skills)
    if skills_block:
        instructions.append(skills_block)
        tools = [*tools, make_load_skill_tool(skills)]
    if enable_delegation and DELEGATE_TOOL_NAME in spec.tools:
        subagents_block = subagent_instructions(
            settings,
            orchestrator_name=spec.agent_dir.name,
            surface=surface,
        )
        if subagents_block:
            instructions.append(subagents_block)
    instructions.append(current_datetime())
    system_prompt = "\n\n".join(str(item) for item in instructions if item)

    model = build_model(
        settings,
        model_settings=spec.model_settings,
        agent_name=spec.name,
    )
    mcp_clients = build_mcp_servers(spec.mcp_servers)
    hooks = build_hooks(spec.hooks)
    decorated_tools = [_to_strands_tool(entry) for entry in tools]
    approval_tool_names = {entry.name for entry in tools if entry.requires_approval}
    agent_kwargs = {
        "model": model.raw,
        "name": spec.name,
        "description": spec.description,
        "system_prompt": system_prompt,
        "tools": decorated_tools,
        "callback_handler": None,
        "interventions": _approval_interventions(
            approval_tool_names,
            surface,
            approve_all=approve_all,
            approval_ask=approval_ask,
        ),
        "hooks": _strands_hook_callbacks(hooks, spec.name),
    }
    raw_agent = Agent(**agent_kwargs)
    return VikramAgent(
        raw_agent=raw_agent,
        agent_kwargs=agent_kwargs,
        name=spec.name,
        description=spec.description,
        model=model,
        system_prompt=system_prompt,
        tools=tools,
        mcp_clients=mcp_clients,
        hooks=hooks,
    )


def _to_strands_tool(entry: VikramTool) -> Any:
    return tool(entry.function, name=entry.name)


def _approval_interventions(
    approval_tool_names: set[str],
    surface: str,
    *,
    approve_all: bool = False,
    approval_ask: Any | None = None,
) -> list[HumanInTheLoop]:
    if not approval_tool_names or approve_all:
        return []
    allowed_tools = ["*", *(f"!{name}" for name in sorted(approval_tool_names))]
    ask = (
        approval_ask
        if approval_ask is not None
        else "stdio" if surface == "cli" else None
    )
    return [HumanInTheLoop(allowed_tools=allowed_tools, ask=ask)]


def _strands_hook_callbacks(hooks: HookSet, agent_name: str) -> list[Any]:
    callbacks: list[Any] = []
    if hooks.pre:

        async def before_tool(event: BeforeToolCallEvent) -> None:
            tool_name, tool_input = _tool_payload(event.tool_use)
            decision = await run_hooks(
                hooks.pre,
                {
                    "event": "PreToolUse",
                    "agent": agent_name,
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "cwd": _cwd(),
                },
                tool_name=tool_name,
            )
            if decision.blocked:
                event.cancel_tool = decision.reason or f"A hook blocked {tool_name}."

        callbacks.append(before_tool)
    if hooks.post:

        async def after_tool(event: AfterToolCallEvent) -> None:
            tool_name, tool_input = _tool_payload(event.tool_use)
            output = _tool_result_text(event.result)
            decision = await run_hooks(
                hooks.post,
                {
                    "event": "PostToolUse",
                    "agent": agent_name,
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "tool_output": output,
                    "cwd": _cwd(),
                },
                tool_name=tool_name,
            )
            if decision.blocked:
                event.result = {
                    "toolUseId": event.tool_use.get("toolUseId", ""),
                    "status": "error",
                    "content": [{"text": decision.reason or "A hook rejected result."}],
                }
                return
            if decision.context:
                content = list(event.result.get("content") or [])
                content.append({"text": decision.context})
                event.result = {**event.result, "content": content}

        callbacks.append(after_tool)
    return callbacks


def _tool_payload(tool_use: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(tool_use, dict):
        return str(tool_use.get("name") or ""), dict(tool_use.get("input") or {})
    return (
        str(getattr(tool_use, "name", "") or ""),
        dict(getattr(tool_use, "input", {}) or {}),
    )


def _tool_result_text(result: Any) -> str:
    if not isinstance(result, dict):
        return str(result)
    rendered: list[str] = []
    for item in result.get("content") or []:
        if isinstance(item, dict):
            if "text" in item:
                rendered.append(str(item["text"]))
            elif "json" in item:
                rendered.append(json.dumps(item["json"], default=str))
    return "\n".join(rendered)


def _settings_with_spec_model(
    settings: VikramSettings, spec: AgentSpec
) -> VikramSettings:
    updates: dict[str, Any] = {}
    if spec.model_provider and settings.model_provider is None:
        updates["model_provider"] = spec.model_provider
    if spec.model and settings.model is None:
        updates["model"] = spec.model
    return settings.model_copy(update=updates) if updates else settings


def __getattr__(name: str) -> VikramAgent:
    """Lazy module-level ``agent`` so importing this module is side-effect-free."""
    if name == "agent":
        return build_agent()
    raise AttributeError(f"module 'vikram.agent' has no attribute {name!r}")
