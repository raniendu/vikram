"""Run one agent against several models at once and compare them.

Fan-out over *models* is safe in a single process, and this is the one place
that is true. The two process-global hazards -- the command policy written by
``set_command_policy`` and the workspace root read from ``Path.cwd()`` -- both
depend on the agent and the workspace, and the playground holds those constant.
Only the model varies, so every column shares an identical policy and cwd.
Fanning out over *agents* would not be safe, and this does not do that.

Approval-gated tools are disabled here. Four columns each asking to approve the
same ``write_file``, then each executing it against one workspace, is both poor
UX and a correctness hazard. Building with no approval callback on a non-CLI
surface reaches the runtime's existing auto-deny branch, so each column reports
the refusal as a failed tool result and the comparison stays honest.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable

from vikram.logging import get_logger

logger = get_logger(__name__)

MIN_COLUMNS = 2
MAX_COLUMNS = 4


class PlaygroundError(RuntimeError):
    """Raised when a comparison cannot be set up."""


@dataclass(frozen=True)
class ColumnSpec:
    provider: str
    model: str

    @property
    def id(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass
class ColumnMetrics:
    column_id: str
    provider: str
    model: str
    ttft_ms: float | None = None
    total_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    tool_calls: int = 0
    output: str = ""
    error: str | None = None


def validate_columns(columns: list[ColumnSpec]) -> None:
    if not MIN_COLUMNS <= len(columns) <= MAX_COLUMNS:
        raise PlaygroundError(
            f"Compare between {MIN_COLUMNS} and {MAX_COLUMNS} models "
            f"(got {len(columns)})."
        )
    seen = [column.id for column in columns]
    if len(set(seen)) != len(seen):
        raise PlaygroundError("Each column must use a different model.")


def build_column_agents(
    spec: Any, settings: Any, columns: list[ColumnSpec]
) -> list[Any]:
    """One agent per column, differing only in the pinned model.

    ``settings.model_provider`` and ``settings.model`` sit at the top of both
    precedence chains in ``resolve_agent_model_selection``, so overriding them
    beats the spec pin without any new precedence logic.
    """
    from vikram.agent import build_agent

    validate_columns(columns)
    agents = []
    for column in columns:
        column_settings = settings.model_copy(
            update={"model_provider": column.provider, "model": column.model}
        )
        agents.append(
            build_agent(
                spec=spec,
                settings=column_settings,
                surface="playground",
                enable_delegation=False,
                # No approval callback: reaches the runtime's auto-deny branch.
                approval_request=None,
            )
        )
    return agents


async def run_column(
    agent: Any,
    column: ColumnSpec,
    prompt: str,
    *,
    emit: Callable[[str, dict[str, Any], str], Awaitable[None]],
) -> ColumnMetrics:
    """Stream one column, tagging every event with its column id."""
    metrics = ColumnMetrics(
        column_id=column.id, provider=column.provider, model=column.model
    )
    started = time.perf_counter()
    try:
        async for raw in agent.stream_events(
            prompt, conversation_id=f"playground:{column.id}"
        ):
            result = raw.get("vikram_result")
            if result is not None:
                metrics.output = str(result.output)
                metrics.total_ms = _ms(started)
                _read_usage(result, metrics)
                await emit("column.finished", asdict(metrics), column.id)
                return metrics

            if "data" in raw or "reasoningText" in raw:
                if metrics.ttft_ms is None:
                    # Measured here rather than at the client, so HTTP and SSE
                    # latency do not pollute the comparison.
                    metrics.ttft_ms = _ms(started)
            if raw.get("current_tool_use"):
                metrics.tool_calls += 1

            from vikram.events import map_stream_event

            event = map_stream_event(raw, seq=0, session_id="", column_id=column.id)
            if event is not None:
                await emit(event.type, event.payload, column.id)
    except asyncio.CancelledError:
        metrics.error = "cancelled"
        metrics.total_ms = _ms(started)
        await emit("column.cancelled", asdict(metrics), column.id)
        raise
    except Exception as exc:
        logger.warning("playground_column_failed", column=column.id, error=str(exc))
        metrics.error = str(exc)
        metrics.total_ms = _ms(started)
        await emit("column.failed", asdict(metrics), column.id)
    return metrics


async def run_comparison(
    agents: list[Any],
    columns: list[ColumnSpec],
    prompt: str,
    *,
    emit: Callable[[str, dict[str, Any], str], Awaitable[None]],
) -> list[ColumnMetrics]:
    """Run every column concurrently and return their metrics."""
    results = await asyncio.gather(
        *(
            run_column(agent, column, prompt, emit=emit)
            for agent, column in zip(agents, columns)
        ),
        return_exceptions=True,
    )
    metrics: list[ColumnMetrics] = []
    for column, result in zip(columns, results):
        if isinstance(result, ColumnMetrics):
            metrics.append(result)
        else:
            metrics.append(
                ColumnMetrics(
                    column_id=column.id,
                    provider=column.provider,
                    model=column.model,
                    error=str(result),
                )
            )
    return metrics


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _read_usage(result: Any, metrics: ColumnMetrics) -> None:
    try:
        usage = result.usage
        if callable(usage):
            usage = usage()
    except Exception as exc:
        logger.warning("playground_usage_unavailable", error=str(exc))
        return
    metrics.input_tokens = getattr(usage, "input_tokens", None)
    metrics.output_tokens = getattr(usage, "output_tokens", None)
    metrics.total_tokens = getattr(usage, "total_tokens", None)


__all__ = [
    "MAX_COLUMNS",
    "MIN_COLUMNS",
    "ColumnMetrics",
    "ColumnSpec",
    "PlaygroundError",
    "build_column_agents",
    "run_column",
    "run_comparison",
    "validate_columns",
]
