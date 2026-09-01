"""Wire events for GUI surfaces.

``VikramAgent.stream_events`` yields loosely-typed dicts shaped for
compatibility with Bedrock/Strands envelopes. That shape is convenient for the
CLI, which renders each event and forgets it, but a GUI needs stable event
names, an ordering guarantee, and a JSON-serialisable payload it can replay.

This module maps the runtime's dicts onto a typed union. It deliberately does
not import FastAPI: the same events travel over a session worker's stdout as
newline-delimited JSON before they ever reach an HTTP response.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from vikram.streaming import tool_results_from_event, tool_use_from_event

EventType = Literal[
    "turn.started",
    "text.delta",
    "thinking.delta",
    "tool.call",
    "tool.result",
    "approval.requested",
    "approval.resolved",
    "turn.finished",
    "turn.failed",
    "turn.cancelled",
    "session.ready",
    "session.closed",
    "heartbeat",
]


class Event(BaseModel):
    """One event on a session (or playground) stream.

    ``seq`` is monotonic per stream so a reconnecting client can resume from a
    ``Last-Event-ID``. ``column_id`` is set only by playground fan-out, where
    several models share one stream.
    """

    type: EventType
    seq: int
    session_id: str
    turn_id: str | None = None
    column_id: str | None = None
    ts: float = Field(default_factory=time.time)
    payload: dict[str, Any] = Field(default_factory=dict)


def map_stream_event(
    raw: dict[str, Any],
    *,
    seq: int,
    session_id: str,
    turn_id: str | None = None,
    column_id: str | None = None,
) -> Event | None:
    """Map one ``stream_events`` dict onto an :class:`Event`.

    Returns ``None`` for events with no GUI meaning, including the terminal
    ``vikram_result`` sentinel -- the caller owns that one, because only it can
    read usage and timing off the result object.
    """

    def _event(type_: EventType, payload: dict[str, Any]) -> Event:
        return Event(
            type=type_,
            seq=seq,
            session_id=session_id,
            turn_id=turn_id,
            column_id=column_id,
            payload=payload,
        )

    text = raw.get("data")
    if isinstance(text, str):
        return _event("text.delta", {"text": text})

    thinking = raw.get("reasoningText")
    if isinstance(thinking, str):
        return _event("thinking.delta", {"text": thinking})

    tool_use = tool_use_from_event(raw)
    if tool_use is not None:
        return _event(
            "tool.call",
            {
                "tool_use_id": tool_use.get("toolUseId"),
                "name": tool_use.get("name"),
                "input": tool_use.get("input") or {},
            },
        )

    results = tool_results_from_event(raw)
    if results:
        result = results[0]
        blocks = result.get("content") or []
        body = "".join(
            block.get("text", "") for block in blocks if isinstance(block, dict)
        )
        return _event(
            "tool.result",
            {
                "tool_use_id": result.get("toolUseId"),
                "status": result.get("status", "success"),
                "text": body,
            },
        )

    return None
