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


class CapabilitiesSpec(_Strict):
    """The ``[capabilities]`` table of ``agent.toml``. Absent entries are off."""

    repair_tool_arguments: bool | None = None
    tool_output_limits: ToolOutputLimitsSpec | None = None
    compaction: CompactionSpec | None = None
    spend_limits: SpendLimitsSpec | None = None


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
    return built


def capability_names(capabilities: list[AbstractCapability[Any]]) -> list[str]:
    """Type names only, for logs: never settings values."""
    return [type(capability).__name__ for capability in capabilities]
