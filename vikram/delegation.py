from __future__ import annotations

import time
from dataclasses import dataclass

from pydantic_ai import Tool

from vikram.logging import get_logger
from vikram.settings import VikramSettings
from vikram.spec import AgentSurfaceError, ensure_surface_allowed
from vikram.tools import ToolEntry

logger = get_logger(__name__)

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
    """List agents the orchestrator can delegate to, from every spec root.

    Skips agents whose spec will not parse. This assembles part of the
    orchestrator's system prompt, so raising here would mean a single malformed
    spec breaks *every* agent's build on every surface -- a real prospect once
    agents are authored in an editor rather than by hand.
    """
    from vikram.specstore import list_agents, load_agent

    subagents: list[SubagentInfo] = []
    for summary in list_agents(settings):
        if summary.id == orchestrator_name:
            continue
        if summary.error is not None:
            logger.warning(
                "subagent_skipped_unreadable_spec",
                agent=summary.id,
                error=summary.error,
            )
            continue
        spec = load_agent(summary.id, settings)
        unavailable_reason = None
        try:
            ensure_surface_allowed(spec, surface)
        except AgentSurfaceError as exc:
            unavailable_reason = str(exc)
        subagents.append(
            SubagentInfo(
                name=summary.id,
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

        from vikram.specstore import load_agent

        target_spec = load_agent(requested_name, settings)

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
            approval_ask=_raise_delegated_approval,
        )
        log = logger.bind(
            tool=DELEGATE_TOOL_NAME,
            orchestrator=orchestrator_name,
            subagent=requested_name,
            surface=surface,
            prompt_length=len(prompt),
        )
        log.info("delegation_started")
        start = time.perf_counter()
        try:
            result = await subagent.run(
                prompt,
                conversation_id=f"delegate:{orchestrator_name}:{requested_name}",
            )
        except DelegatedApprovalRequired as exc:
            log.info(
                "delegation_stopped",
                duration_ms=_elapsed_ms(start),
                reason="approval_required",
            )
            return (
                f"Subagent {target_spec.name} stopped because it {exc}. "
                "Run that agent directly when the task needs approval-gated "
                "tools, or delegate a read-only task."
            )
        except Exception:
            log.exception("delegation_failed", duration_ms=_elapsed_ms(start))
            raise
        log.info(
            "delegation_succeeded",
            duration_ms=_elapsed_ms(start),
            output_length=len(str(result.output)),
        )
        return f"Subagent {target_spec.name} completed.\n\n{result.output}"

    return Tool(
        delegate_to_agent,
        name=DELEGATE_TOOL_NAME,
        requires_approval=requires_approval,
        sequential=True,
    )


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


async def _raise_delegated_approval(_: str) -> str:
    raise DelegatedApprovalRequired("requested an approval-gated tool call")
