from __future__ import annotations

import asyncio
import inspect
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from pydantic_ai import (
    Agent,
    AgentRunResultEvent,
    DeferredToolRequests,
    DeferredToolResults,
    RunContext,
    Tool,
    ToolDenied,
)
from pydantic_ai.capabilities import HandleDeferredToolCalls
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
)
from pydantic_ai.toolsets import CombinedToolset, FunctionToolset

from vikram.context import agent_identity, current_datetime
from vikram.delegation import (
    DELEGATE_TOOL_NAME,
    make_delegate_to_agent_tool,
    subagent_instructions,
)
from vikram.hooks import HookBlockedError, HookSet, HookToolset, build_hooks, run_hooks
from vikram.logging import get_logger
from vikram.mcp import VikramMCPClient, build_mcp_servers
from vikram.settings import (
    VikramModel,
    VikramSettings,
    build_model,
    resolve_agent_model_selection,
)
from vikram.skills import discover_skills, make_load_skill_tool, skills_instructions
from vikram.spec import AgentSpec, load_spec
from vikram.tools import TOOL_REGISTRY, ToolEntry, set_command_policy

logger = get_logger(__name__)


class AgentToolError(RuntimeError):
    """Raised when an agent spec references tools unavailable to this package."""


ApprovalAsk = Callable[[str], str | Awaitable[str]]


class VikramAgent:
    """Stable Vikram interface backed by a Pydantic AI agent."""

    runtime = "pydantic-ai"

    def __init__(
        self,
        *,
        raw_agent: Agent[None, str],
        name: str,
        description: str,
        model: VikramModel,
        system_prompt: str,
        tools: list[ToolEntry],
        mcp_clients: list[VikramMCPClient],
        hooks: HookSet,
    ) -> None:
        self.raw_agent = raw_agent
        self.name = name
        self.description = description
        self.model = model.raw
        self.model_config = model.config
        self.system_prompt = system_prompt
        self.tools = tools
        self.tool_names = [_tool_name(entry) for entry in tools]
        self.approval_tool_names = [
            _tool_name(entry) for entry in tools if _requires_approval(entry)
        ]
        self.mcp_clients = mcp_clients
        self._hookset = hooks

    async def __aenter__(self) -> VikramAgent:
        await self.raw_agent.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> Any:
        return await self.raw_agent.__aexit__(*args)

    async def run(
        self,
        user_prompt: str,
        *,
        message_history: list[Any] | None = None,
        conversation_id: str | None = None,
        **kwargs: Any,
    ) -> Any:
        prompt = await self._apply_user_prompt_hooks(user_prompt)
        result = await self.raw_agent.run(
            prompt,
            message_history=message_history,
            conversation_id=conversation_id,
            **kwargs,
        )
        await self._run_stop_hooks(result)
        return result

    def run_sync(self, user_prompt: str, **kwargs: Any) -> Any:
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
        result: Any | None = None
        async with self.raw_agent.run_stream_events(
            prompt,
            message_history=message_history,
            conversation_id=conversation_id,
            **kwargs,
        ) as events:
            async for event in events:
                if isinstance(event, AgentRunResultEvent):
                    result = event.result
                    continue
                mapped = _stream_event(event)
                if mapped is not None:
                    yield mapped

        if result is None:  # Defensive: Pydantic AI documents a final result event.
            raise RuntimeError("Pydantic AI stream ended without a final result.")
        await self._run_stop_hooks(result)
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
                "cwd": os.getcwd(),
            },
        )
        if decision.blocked:
            logger.warning(
                "hook_blocked_prompt", agent=self.name, hook_event="UserPromptSubmit"
            )
            raise HookBlockedError(decision.reason or "A hook blocked this prompt.")
        if decision.context:
            logger.info(
                "hook_added_prompt_context",
                agent=self.name,
                hook_event="UserPromptSubmit",
                context_length=len(decision.context),
            )
            return f"{decision.context}\n\n{user_prompt}"
        return user_prompt

    async def _run_stop_hooks(self, result: Any) -> None:
        if not self._hookset.stop:
            return
        await run_hooks(
            self._hookset.stop,
            {
                "event": "Stop",
                "agent": self.name,
                "output": str(result.output),
                "cwd": os.getcwd(),
            },
        )


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
        logger.error(
            "agent_tools_unresolved",
            agent=spec.name,
            surface=surface,
            missing_tools=missing,
        )
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
    approval_ask: ApprovalAsk | None = None,
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
    command_policy = spec.load_command_policy()
    set_command_policy(command_policy)

    skills = discover_skills(spec)
    instructions: list[str] = [spec.instructions, agent_identity(spec.name)]
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
    system_prompt = "\n\n".join(item for item in instructions if item)

    model = build_model(
        settings,
        model_settings=spec.model_settings,
        agent_name=spec.name,
    )
    mcp_clients = build_mcp_servers(spec.mcp_servers)
    hooks = build_hooks(spec.hooks)

    base_toolset = FunctionToolset(tools)
    all_toolsets = [base_toolset, *(client.raw for client in mcp_clients)]
    combined = CombinedToolset(all_toolsets)
    toolset = (
        HookToolset(combined, pre=hooks.pre, post=hooks.post, agent_name=spec.name)
        if hooks.has_tool_hooks
        else combined
    )

    capabilities = [
        HandleDeferredToolCalls(
            handler=_approval_handler(
                surface=surface,
                approve_all=approve_all,
                approval_ask=approval_ask,
            ),
            id="vikram-human-approval",
        )
    ]
    raw_agent = Agent(
        model.raw,
        name=spec.name,
        description=spec.description,
        instructions=system_prompt,
        model_settings=spec.model_settings or None,
        toolsets=[toolset],
        capabilities=capabilities,
    )
    logger.info(
        "agent_built",
        agent=spec.name,
        surface=surface,
        model_provider=model.config.get("provider"),
        model=model.config.get("model"),
        tools=sorted(_tool_name(entry) for entry in tools),
        mcp_servers=[client.id for client in mcp_clients],
        skills=[skill.name for skill in skills],
        hook_events=_configured_hook_events(hooks),
        command_policy_deny_rules=len(command_policy.deny),
        approve_all=approve_all,
        system_prompt_length=len(system_prompt),
    )
    return VikramAgent(
        raw_agent=raw_agent,
        name=spec.name,
        description=spec.description,
        model=model,
        system_prompt=system_prompt,
        tools=tools,
        mcp_clients=mcp_clients,
        hooks=hooks,
    )


def _configured_hook_events(hooks: HookSet) -> list[str]:
    events = {
        "PreToolUse": hooks.pre,
        "PostToolUse": hooks.post,
        "UserPromptSubmit": hooks.user_prompt_submit,
        "Stop": hooks.stop,
    }
    return [event for event, configured in events.items() if configured]


def _approval_handler(
    *,
    surface: str,
    approve_all: bool,
    approval_ask: ApprovalAsk | None,
) -> Callable[[RunContext[None], DeferredToolRequests], Awaitable[DeferredToolResults]]:
    async def handle(
        _: RunContext[None], requests: DeferredToolRequests
    ) -> DeferredToolResults:
        if approve_all:
            return requests.build_results(approve_all=True)

        approvals: dict[str, bool | ToolDenied] = {}
        for call in requests.approvals:
            prompt = (
                f'Tool "{call.tool_name}" requires human approval. Input: '
                f"{json.dumps(call.args_as_dict(), default=str)}"
            )
            if approval_ask is not None:
                answer = approval_ask(prompt)
                if inspect.isawaitable(answer):
                    answer = await answer
            elif surface == "cli":
                answer = await asyncio.to_thread(input, f"{prompt} [y/N] ")
            else:
                approvals[call.tool_call_id] = ToolDenied(
                    "Approval is unavailable on this interface."
                )
                continue
            approvals[call.tool_call_id] = str(answer).strip().lower() in {
                "y",
                "yes",
                "allow",
                "approve",
            }
        return requests.build_results(approvals=approvals, calls={})

    return handle


def _stream_event(event: Any) -> dict[str, Any] | None:
    if isinstance(event, PartStartEvent):
        if isinstance(event.part, TextPart) and event.part.content:
            return {"data": event.part.content}
        if isinstance(event.part, ThinkingPart) and event.part.content:
            return {"reasoningText": event.part.content}
        return None
    if isinstance(event, PartDeltaEvent):
        if isinstance(event.delta, TextPartDelta) and event.delta.content_delta:
            return {"data": event.delta.content_delta}
        if isinstance(event.delta, ThinkingPartDelta) and event.delta.content_delta:
            return {"reasoningText": event.delta.content_delta}
        return None
    if isinstance(event, FunctionToolCallEvent):
        part = event.part
        return {
            "current_tool_use": {
                "toolUseId": part.tool_call_id,
                "name": part.tool_name,
                "input": part.args_as_dict(),
            }
        }
    if isinstance(event, FunctionToolResultEvent):
        part = event.part
        outcome = getattr(part, "outcome", "success")
        content = event.content if event.content is not None else part.content
        return {
            "tool_result": {
                "toolUseId": part.tool_call_id,
                "status": "error" if outcome in {"failed", "denied"} else "success",
                "content": [{"text": _stringify_content(content)}],
            }
        }
    return None


def _stringify_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_stringify_content(item) for item in content)
    return str(content)


def _tool_name(entry: ToolEntry) -> str:
    if isinstance(entry, Tool):
        return entry.name
    return entry.__name__


def _requires_approval(entry: ToolEntry) -> bool:
    return isinstance(entry, Tool) and entry.requires_approval


def _settings_with_spec_model(
    settings: VikramSettings, spec: AgentSpec
) -> VikramSettings:
    """Resolve the agent's provider/model onto settings.

    Precedence: explicit env/CLI (``model_provider``/``model`` already set) >
    the user's saved per-agent choice (``[agents.<id>]`` in config.toml,
    written by ``/model``) > the spec's pinned provider/model > the config
    file's global ``default_provider``. A saved or pinned model only applies
    when its provider matches the resolved provider, so an env provider
    switch never pairs another provider's model. When no model resolves
    here, ``build_model`` falls back to the resolved provider's own model
    from ``[providers.<id>]``.
    """
    provider, model = resolve_agent_model_selection(
        settings,
        agent_id=spec.agent_dir.name,
        spec_provider=spec.model_provider,
        spec_model=spec.model,
    )

    updates: dict[str, Any] = {}
    if provider != settings.model_provider:
        updates["model_provider"] = provider
    if model != settings.model:
        updates["model"] = model
    return settings.model_copy(update=updates) if updates else settings
