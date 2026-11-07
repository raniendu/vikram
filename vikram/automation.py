"""Public structured output: content is emitted only as requested protocol data."""

import json
import sys


async def json_reply(session, prompt: str) -> None:
    def emit(event):
        sys.stdout.write(json.dumps({"v": 1, **event}, ensure_ascii=True) + "\n")
        sys.stdout.flush()

    emit({"event": "session", "id": session.session_id, "model": session.model})
    async for event in session.reply(prompt):
        # Display thinking is deliberately absent from automation artifacts.
        emit(
            {
                "event": "reply",
                **{
                    key: value
                    for key, value in event.items()
                    if key not in {"reasoning", "reasoning_truncated"}
                },
            }
        )
    emit({"event": "complete", "session_id": session.session_id})
