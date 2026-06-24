from __future__ import annotations

from dataclasses import dataclass

from vikram.settings import VikramSettings
from vikram.spec import AgentSpec, AgentSurfaceError, ensure_surface_allowed, load_spec
from vikram.tools import ToolEntry, VikramTool

DELEGATE_TOOL_NAME = "delegate_to_agent"


class DelegatedApprovalRequired(RuntimeError):
    """Raised when a delegated run requests approval-gated tool calls."""


@dataclass(frozen=True)
class SubagentInfo:
    name: str
    display_name: str
    description: str
    cli_only: bool
    available: bool
    unavailable_reason: str | None = None


def discover_subagents(
    settings: VikramSettings,
    *,
    orchestrator_name: str,
    surface: str,
) -> list[SubagentInfo]:
    """List checked-in agents the orchestrator can delegate to."""
    subagents: list[SubagentInfo] = []
    for spec_path in sorted(settings.spec_root.glob("*/agent.toml")):
        agent_name = spec_path.parent.name
        if agent_name == orchestrator_name:
            continue
        spec = load_spec(agent_name, settings.spec_root)
        unavailable_reason = None
        try:
            ensure_surface_allowed(spec, surface)
        except AgentSurfaceError as exc:
            unavailable_reason = str(exc)
        subagents.append(
            SubagentInfo(
                name=agent_name,
                display_name=spec.name,
                description=spec.description,
                cli_only=spec.cli_only,
                available=unavailable_reason is None,
                unavailable_reason=unavailable_reason,
            )
        )
    return subagents


def subagent_instructions(
    settings: VikramSettings,
    *,
    orchestrator_name: str,
    surface: str,
) -> str:
    subagents = discover_subagents(
        settings, orchestrator_name=orchestrator_name, surface=surface
    )
    if not subagents:
        return ""

    lines = [
        "## Available subagents",
        "",
        "You can delegate specialized work to another Vikram agent by calling "
        f"`{DELEGATE_TOOL_NAME}` with the agent name and a self-contained "
        "prompt. Use delegation when another agent is a better fit for the "
        "task, then synthesize the subagent's result for the user.",
        "",
    ]
    for subagent in subagents:
        suffix = ""
        if not subagent.available:
            suffix = f" (unavailable on this surface: {subagent.unavailable_reason})"
        elif subagent.cli_only:
            suffix = " (local CLI/ACP only)"
        lines.append(f"- `{subagent.name}`: {subagent.description}{suffix}")
    return "\n".join(lines)


def make_delegate_to_agent_tool(
    *,
    settings: VikramSettings,
    orchestrator_name: str,
    surface: str,
    requires_approval: bool,
) -> ToolEntry:
    async def delegate_to_agent(agent_name: str, prompt: str) -> str:
        """Delegate a self-contained task to another configured Vikram agent.

        Use this when a specialized agent is a better fit for a task. The
        prompt must include all context the subagent needs; it does not receive
        the parent conversation history. The subagent's answer is returned to
        you so you can decide what to tell the user.

        Args:
            agent_name: The spec directory name of the target agent, such as
                "coder".
            prompt: A complete task prompt for the subagent.
        """
        requested_name = agent_name.strip()
        if not requested_name:
            return "Cannot delegate: agent_name is required."
        if requested_name == orchestrator_name:
            return f"Cannot delegate: {orchestrator_name} cannot delegate to itself."
        if not prompt.strip():
            return "Cannot delegate: prompt is required."

        subagents = discover_subagents(
            settings, orchestrator_name=orchestrator_name, surface=surface
        )
        subagent_names = {subagent.name for subagent in subagents}
        if requested_name not in subagent_names:
            available = ", ".join(subagent.name for subagent in subagents)
            return (
                f"Unknown agent {requested_name!r}. "
                f"Available subagents: {available or '(none)'}."
            )

        target_spec = load_spec(requested_name, settings.spec_root)

        try:
            ensure_surface_allowed(target_spec, surface)
        except AgentSurfaceError as exc:
            return f"Cannot delegate to {requested_name!r}: {exc}"

        from vikram.agent import build_agent

        subagent = build_agent(
            spec=target_spec,
            settings=settings,
            surface=surface,
            enable_delegation=False,
        )
        try:
            result = await subagent.run(
                prompt,
                conversation_id=f"delegate:{orchestrator_name}:{requested_name}",
            )
        except (DelegatedApprovalRequired, RuntimeError) as exc:
            return (
                f"Subagent {target_spec.name} stopped because it {exc}. "
                "Run that agent directly when the task needs approval-gated "
                "tools, or delegate a read-only task."
            )
        return f"Subagent {target_spec.name} completed.\n\n{result.output}"

    return VikramTool(
        DELEGATE_TOOL_NAME,
        delegate_to_agent,
        requires_approval=requires_approval,
    )
