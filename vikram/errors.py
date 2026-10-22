"""Actionable model failure messages without prompts or provider response bodies."""

import httpx
from openai import APIConnectionError, APITimeoutError
from pydantic_ai.exceptions import AgentRunError, ModelHTTPError


class ReplyError(Exception):
    """A conversation failure containing only a safe user-facing description."""


def reply_error(error: Exception) -> str:
    if isinstance(error, ReplyError):
        return str(error)
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
            "Ollama timed out while Vikram was waiting for a reply. It may be busy "
            "with another app or loading the model. Let other model requests finish "
            "and retry, or choose a smaller model."
        )
    if isinstance(error, ModelHTTPError):
        if error.status_code == 404:
            return (
                "The selected model is not available in Ollama. Refresh the model "
                "list, install it in Ollama, or choose another local model."
            )
        if error.status_code in (429, 503):
            return (
                "Ollama is busy and could not accept this request. "
                "Let other model requests finish, then retry."
            )
        return (
            f"Ollama rejected this request (HTTP {error.status_code}). "
            "Check Ollama and retry, or choose another model."
        )
    if any(isinstance(c, (httpx.ConnectError, APIConnectionError)) for c in causes):
        return "Could not connect to local Ollama. Open Ollama, then retry."
    if isinstance(error, AgentRunError):
        return "Ollama could not complete this reply. Retry or choose another model."
    return (
        f"Vikram encountered an internal error ({type(error).__name__}). "
        "Quit and restart the installed app, then retry. "
        "If this continues, reinstall Vikram."
    )
