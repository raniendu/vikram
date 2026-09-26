"""Spec-driven Pydantic AI Harness capabilities.

``[capabilities]`` in an agent's ``agent.toml`` switches on building blocks
from `pydantic-ai-harness <https://github.com/pydantic/pydantic-ai-harness>`_.
Every entry is optional and off when absent, so a spec without the table
builds exactly the agent it always did. See ``docs/capabilities.md``.

Example::

    [capabilities]
    repair_tool_arguments = true

    [capabilities.compaction]
    strategy = "summarizing"
    max_tokens = 24000
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt

from vikram.logging import get_logger

if TYPE_CHECKING:
    from pydantic_ai.capabilities import AbstractCapability

    from vikram.spec import AgentSpec

logger = get_logger(__name__)

# Mirrors vikram.spec.NETWORK_SURFACES; duplicated to avoid an import cycle
# (spec.py imports this module for the schema).
_NETWORK_SURFACES = ("http", "telegram", "threaded")


class _Strict(BaseModel):
    # A misspelt key would otherwise silently leave a capability off.
    model_config = ConfigDict(extra="forbid")


class CompactionSpec(_Strict):
    """Keep long conversations inside the model's context window."""

    strategy: Literal["summarizing", "sliding_window"] = "summarizing"
    max_tokens: PositiveInt = Field(
        default=24_000,
        description="Compact once the conversation history passes this size.",
    )
    keep_messages: PositiveInt = Field(
        default=20, description="Recent messages always kept verbatim."
    )


class ToolOutputLimitsSpec(_Strict):
    """Cap every tool result (built-in, MCP, delegated) before the model sees it."""

    max_chars: PositiveInt = 40_000
    strategy: Literal["head", "tail", "head_tail"] = "head_tail"


class SpendLimitsSpec(_Strict):
    """Stop a run once it has spent its budget.

    Local models price at zero, so token budgets are the ones that bite for
    Ollama. Budgets live in process memory.
    """

    tokens_per_run: PositiveInt | None = None
    usd_per_run: Decimal | None = Field(default=None, gt=0)
    usd_per_day: Decimal | None = Field(default=None, gt=0)
    surfaces: list[str] = Field(
        default_factory=lambda: list(_NETWORK_SURFACES),
        description="Surfaces the budgets apply to.",
    )


class PromptInjectionSpec(_Strict):
    """Scan tool results for instructions planted in web pages, files, MCP output.

    Local pattern detection, no network. ``report`` only logs detections;
    ``block`` also withholds high-risk results from the model.
    """

    mode: Literal["report", "block"] = "report"
    tools: list[str] = Field(
        default_factory=list, description="Tools to scan; empty means all."
    )


class GuardrailsSpec(_Strict):
    """Redact known secret formats (API keys, tokens, private keys) and refuse
    prompts containing blocked keywords."""

    redact_secrets: list[Literal["input", "output", "tool_results"]] = Field(
        default_factory=list,
        description="Where to redact secrets: user input, the reply, tool results.",
    )
    blocked_keywords: list[str] = Field(
        default_factory=list, description="Prompts containing these are refused."
    )


class MemorySpec(_Strict):
    """A persistent notebook the agent reads and writes across conversations.

    Stored locally in SQLite. ``conversation`` keeps a separate notebook per
    chat (keyed by a hash of the conversation id); ``agent`` shares one.
    """

    scope: Literal["conversation", "agent"] = "conversation"
    database: str = ".vikram/memory.sqlite3"
    max_tokens: PositiveInt = Field(
        default=2_000, description="Budget for memory injected into each request."
    )


class CapabilitiesSpec(_Strict):
    """The ``[capabilities]`` table of ``agent.toml``. Absent entries are off."""

    repair_tool_arguments: bool | None = None
    tool_output_limits: ToolOutputLimitsSpec | None = None
    compaction: CompactionSpec | None = None
    spend_limits: SpendLimitsSpec | None = None
    prompt_injection: PromptInjectionSpec | None = None
    guardrails: GuardrailsSpec | None = None
    memory: MemorySpec | None = None


def _compaction(spec: CompactionSpec) -> AbstractCapability[Any]:
    from pydantic_ai_harness import SlidingWindowCompaction, SummarizingCompaction

    if spec.strategy == "sliding_window":
        return SlidingWindowCompaction(
            max_tokens=spec.max_tokens, keep_messages=spec.keep_messages
        )
    # The summary is written by the agent's own model, so local specs stay local.
    return SummarizingCompaction(
        max_tokens=spec.max_tokens, keep_messages=spec.keep_messages
    )


def _tool_output_limits(spec: ToolOutputLimitsSpec) -> AbstractCapability[Any]:
    from pydantic_ai_harness import ToolOutputLimits
    from pydantic_ai_harness.tool_output_limits import (
        Band,
        Truncate,
        TruncationStrategy,
    )

    # Plain truncation. The harness default spills oversized output to local
    # files, which a network surface should not be writing on a caller's behalf.
    action = Truncate(
        strategy=TruncationStrategy(spec.strategy), max_chars=spec.max_chars
    )
    return ToolOutputLimits(bands=[Band(over=spec.max_chars, action=action)])


def _spend_limits(spec: SpendLimitsSpec) -> AbstractCapability[Any] | None:
    from pydantic_ai_harness import SpendLimits
    from pydantic_ai_harness.spend import Budget

    budgets = []
    if spec.tokens_per_run:
        budgets.append(
            Budget(tokens=spec.tokens_per_run, window="run", name="tokens_per_run")
        )
    if spec.usd_per_run:
        budgets.append(Budget(usd=spec.usd_per_run, window="run", name="usd_per_run"))
    if spec.usd_per_day:
        budgets.append(Budget(usd=spec.usd_per_day, window="day", name="usd_per_day"))
    return SpendLimits(budgets=budgets) if budgets else None


def _prompt_injection(spec: PromptInjectionSpec) -> AbstractCapability[Any]:
    from pydantic_ai_harness import PromptInjectionDefender

    def on_detection(ctx: Any, call: Any, result: Any) -> None:
        # Tool name and risk only: the flagged content stays out of logs.
        logger.warning(
            "prompt_injection_detected",
            tool=getattr(call, "tool_name", None),
            risk_level=str(getattr(result, "risk_level", "")),
            allowed=getattr(result, "allowed", None),
            detection_count=len(getattr(result, "detections", None) or []),
        )

    return PromptInjectionDefender(
        block_high_risk=spec.mode == "block",
        tool_filter=spec.tools or "all",
        on_detection=on_detection,
    )


def _guardrails(spec: GuardrailsSpec) -> list[AbstractCapability[Any]]:
    from pydantic_ai_harness import InputGuardrail, OutputGuardrail, ToolGuardrail
    from pydantic_ai_harness.guardrails.detectors import (
        blocked_keywords,
        for_text,
        for_tool_result_text,
        redact_secrets,
    )

    built: list[AbstractCapability[Any]] = []
    input_guards: list[Any] = []
    if spec.blocked_keywords:
        input_guards.append(
            for_text(blocked_keywords(spec.blocked_keywords), on_other="allow")
        )
    if "input" in spec.redact_secrets:
        input_guards.append(for_text(redact_secrets, on_other="allow"))
    if input_guards:
        built.append(InputGuardrail(guard=input_guards))
    if "tool_results" in spec.redact_secrets:
        built.append(
            ToolGuardrail(
                result_guard=for_tool_result_text(redact_secrets, on_other="allow")
            )
        )
    if "output" in spec.redact_secrets:
        built.append(OutputGuardrail(guard=for_text(redact_secrets, on_other="allow")))
    return built


def _conversation_namespace(ctx: Any) -> str:
    """A stable, non-reversible notebook key per conversation."""
    import hashlib

    conversation = getattr(ctx, "conversation_id", None) or "default"
    return hashlib.sha256(str(conversation).encode()).hexdigest()[:16]


def _memory(spec: MemorySpec, agent_name: str) -> AbstractCapability[Any]:
    from pathlib import Path

    from pydantic_ai_harness.memory import Memory, SqliteMemoryStore

    database = Path(spec.database)
    database.parent.mkdir(parents=True, exist_ok=True)
    return Memory(
        store=SqliteMemoryStore(database=database),
        agent_name=agent_name,
        namespace=_conversation_namespace if spec.scope == "conversation" else "",
        max_tokens=spec.max_tokens,
    )


def build_capabilities(
    spec: AgentSpec, *, surface: str
) -> list[AbstractCapability[Any]]:
    """Harness capabilities enabled by ``spec`` for an agent on ``surface``."""
    config = spec.capabilities
    built: list[AbstractCapability[Any]] = []
    if config.repair_tool_arguments:
        from pydantic_ai_harness.repair_tool_arguments import RepairToolArguments

        built.append(RepairToolArguments())
    if config.tool_output_limits is not None:
        built.append(_tool_output_limits(config.tool_output_limits))
    if config.compaction is not None:
        built.append(_compaction(config.compaction))
    if config.spend_limits is not None and surface in config.spend_limits.surfaces:
        limits = _spend_limits(config.spend_limits)
        if limits is not None:
            built.append(limits)
    if config.prompt_injection is not None:
        built.append(_prompt_injection(config.prompt_injection))
    if config.guardrails is not None:
        built.extend(_guardrails(config.guardrails))
    if config.memory is not None:
        built.append(_memory(config.memory, spec.agent_dir.name))
    return built


def capability_names(capabilities: list[AbstractCapability[Any]]) -> list[str]:
    """Type names only, for logs: never settings values."""
    return [type(capability).__name__ for capability in capabilities]
