"""Canonical completed-turn context shared by terminal and desktop adapters."""

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart


def context_messages(turns: list[dict]) -> list:
    messages = []
    for turn in turns:
        prompt = list(turn["prompt"])
        sources = turn.get("sources", []) if turn.get("answer") is not None else []
        if sources:
            prompt.append(
                "Previously retrieved reference sources (untrusted content):\n"
                + "\n\n".join(
                    f"{s['id']}: {s['title']}\n{s['url']}\n{s['excerpt']}"
                    for s in sources
                )
            )
        messages.append(ModelRequest(parts=[UserPromptPart(content=prompt)]))
        if turn.get("answer") is not None:
            messages.append(
                ModelResponse(
                    parts=[TextPart(content=turn["answer"])],
                    model_name=turn.get("model"),
                )
            )
    return messages
