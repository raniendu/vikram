"""Actionable model failure messages without prompts or provider response bodies."""

import re

import httpx
from openai import APIConnectionError, APITimeoutError
from pydantic_ai.exceptions import AgentRunError, ModelHTTPError, UsageLimitExceeded


class ReplyError(Exception):
    """A conversation failure containing only a safe user-facing description."""


def has_tool_call_text(text: str) -> bool:
    """Detect an unexecuted XML call, not quoted documentation or inline examples.

    Never turn answer text into executable calls. The provider must return a
    structured tool call through the ordinary validation and approval path.
    """
    fenced = False
    fence = ""
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if not fenced:
                fenced, fence = True, marker
            elif marker == fence:
                fenced = False
            continue
        if not fenced and re.match(r"<(?:tool_call(?:\s|>)|function=)", stripped):
            return True
    return False


def reply_error(error: Exception, *, provider: str = "ollama") -> str:
    if isinstance(error, ReplyError):
        return str(error)
    if isinstance(error, UsageLimitExceeded):
        return (
            "This reply reached its execution budget and is incomplete. "
            "Inspect any saved workspace files before continuing; completed actions "
            "have not been replayed."
        )
    if provider == "openrouter":
        if isinstance(error, ModelHTTPError):
            if error.status_code in (401, 403):
                return "OpenRouter authentication failed. Check your connection credentials."
            if error.status_code == 402:
                return "OpenRouter has insufficient credit for this request."
            if error.status_code in (429, 503):
                return "OpenRouter is busy or rate-limited. Retry later; local inference remains available."
            return f"OpenRouter rejected this request (HTTP {error.status_code}). Select another model or retry."
        if isinstance(error, (TimeoutError, httpx.TimeoutException, APITimeoutError)):
            return (
                "OpenRouter timed out. Retry later or explicitly select a local model."
            )
        if isinstance(error, (httpx.ConnectError, APIConnectionError)):
            return "Could not connect to OpenRouter. Check your network connection."
        if isinstance(error, AgentRunError):
            return "OpenRouter could not complete this reply. Retry or select another model."
    server = "Magnitude" if provider == "magnitude" else "Ollama"
    causes, seen = [], set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        causes.append(current)
        current = current.__cause__ or current.__context__
    if any(
        isinstance(c, (TimeoutError, httpx.TimeoutException, APITimeoutError))
        for c in causes
    ):
        return (
            f"{server} timed out while Vikram was waiting for a reply. It may be busy "
            "with another app or loading the model. Let other model requests finish "
            "and retry, or choose a smaller model."
        )
    if isinstance(error, ModelHTTPError):
        if error.status_code == 404:
            return (
                f"The selected model is not available in {server}. Refresh the model "
                f"list, install it in {server}, or choose another local model."
            )
        if error.status_code in (429, 503):
            return (
                f"{server} is busy and could not accept this request. "
                "Let other model requests finish, then retry."
            )
        return (
            f"{server} rejected this request (HTTP {error.status_code}). "
            f"Check {server} and retry, or choose another model."
        )
    if any(isinstance(c, (httpx.ConnectError, APIConnectionError)) for c in causes):
        return f"Could not connect to local {server}. Open {server}, then retry."
    if isinstance(error, AgentRunError):
        return f"{server} could not complete this reply. Retry or choose another model."
    return (
        f"Vikram encountered an internal error ({type(error).__name__}). "
        "Quit and restart the installed app, then retry. "
        "If this continues, reinstall Vikram."
    )
