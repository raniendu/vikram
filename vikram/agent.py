from __future__ import annotations

from typing import Any

from pydantic_ai import Agent

from vikram.context import agent_identity, current_datetime
from vikram.mcp import build_mcp_servers
from vikram.settings import VikramSettings, build_model
from vikram.skills import discover_skills, make_load_skill_tool, skills_instructions
from vikram.spec import AgentSpec, load_spec
from vikram.tools import TOOL_REGISTRY, ToolEntry, set_command_policy


class AgentToolError(RuntimeError):
    """Raised when an agent spec references tools unavailable to this package."""


def _resolve_tools(spec: AgentSpec) -> list[ToolEntry]:
    missing = [name for name in spec.tools if name not in TOOL_REGISTRY]
    if missing:
        missing_list = ", ".join(missing)
        raise AgentToolError(
            f"Agent {spec.name} references unknown tool(s): {missing_list}. "
            "The installed Vikram package may be stale relative to the agent "
            "specs; run `vikram update` or reinstall the vikram uv tool."
        )
    return [TOOL_REGISTRY[name] for name in spec.tools]


def build_agent(
    spec: AgentSpec | None = None,
    settings: VikramSettings | None = None,
) -> Agent[None, str]:
    settings = settings or VikramSettings()
    spec = spec or load_spec(settings.default_agent, settings.spec_root)
    tools = _resolve_tools(spec)
    set_command_policy(spec.load_command_policy())

    # Skills are progressively disclosed: only names + descriptions go in the
    # instructions; the load_skill tool reveals full bodies on demand.
    skills = discover_skills(spec)
    instructions: list[Any] = [spec.instructions, agent_identity(spec.name)]
    skills_block = skills_instructions(skills)
    if skills_block:
        instructions.append(skills_block)
        tools = [*tools, make_load_skill_tool(skills)]
    instructions.append(current_datetime)

    # MCP servers are toolsets; Pydantic AI starts and stops them automatically
    # for each agent run, so no explicit lifecycle management is needed here.
    mcp_servers = build_mcp_servers(spec.mcp_servers)

    return Agent(
        build_model(settings),
        name=spec.name,
        description=spec.description,
        instructions=instructions,
        tools=tools,
        toolsets=mcp_servers or None,
        model_settings=spec.model_settings or None,
    )


def __getattr__(name: str) -> Agent[None, str]:
    """Lazy module-level ``agent`` so importing this module is side-effect-free.

    The default-agent singleton is built only when something actually reads
    ``vikram.agent.agent`` (currently just one test). This keeps fast paths
    like ``vikram update`` and ``vikram --version`` from triggering a model
    build at import time.
    """
    if name == "agent":
        return build_agent()
    raise AttributeError(f"module 'vikram.agent' has no attribute {name!r}")
