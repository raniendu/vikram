from __future__ import annotations

from datetime import datetime


def agent_identity(name: str) -> str:
    return f"Your name is {name}."


def current_datetime() -> str:
    now = datetime.now().astimezone()
    return (
        f"Current date and time: {now.isoformat(timespec='seconds')} "
        f"({now.tzname()})."
    )
