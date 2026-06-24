from __future__ import annotations

from typing import Any


def tool_use_from_event(event: dict[str, Any]) -> dict[str, Any] | None:
    current = event.get("current_tool_use")
    if isinstance(current, dict):
        return current
    tool_use = (
        event.get("event", {})
        .get("contentBlockStart", {})
        .get("start", {})
        .get("toolUse")
    )
    return tool_use if isinstance(tool_use, dict) else None


def tool_results_from_event(event: dict[str, Any]) -> list[dict[str, Any]]:
    direct = event.get("tool_result") or event.get("toolResult")
    if isinstance(direct, dict):
        return [direct]

    message = event.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return []

    results: list[dict[str, Any]] = []
    for item in message.get("content") or []:
        if not isinstance(item, dict):
            continue
        result = item.get("toolResult")
        if isinstance(result, dict):
            results.append(result)
    return results


def tool_result_from_event(event: dict[str, Any]) -> dict[str, Any] | None:
    results = tool_results_from_event(event)
    return results[0] if results else None
