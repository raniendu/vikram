"""Canonical completed-turn context shared by terminal and desktop adapters."""

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from vikram.toolkit import bounded

ANSWER_STATUS = {
    "declined": "declined to answer",
    "timed_out": "did not answer in time",
    "stopped": "did not answer",
}
PLAN_STATUS = {
    "approved": "approved by the user",
    "changes_requested": "the user requested changes",
    "not_approved": "not approved yet",
    "timed_out": "not reviewed in time",
    "stopped": "not reviewed",
}


def activity_note(activity: list[dict]) -> str | None:
    """Questions, the latest plan, and recent sandbox runs, for follow-up turns."""
    lines = []
    for item in activity:
        if item.get("kind") != "question":
            continue
        answers = item.get("answers") or {}
        for question in item.get("questions", []):
            picked = answers.get(question["header"])
            lines.append(
                f"- You asked: {question['question']}\n  User: "
                + (
                    ", ".join(picked)
                    if picked
                    else ANSWER_STATUS.get(item["status"], "no answer")
                )
            )
    plans = [item for item in activity if item.get("kind") == "plan"]
    if plans:
        plan = plans[-1]
        lines.append(
            f"- Your latest plan ({PLAN_STATUS.get(plan['status'], 'not reviewed')}):"
            f"\n{plan['plan']}"
            + (f"\n  User feedback: {plan['feedback']}" if plan.get("feedback") else "")
        )
    runs = [
        item
        for item in activity
        if item.get("kind") == "code" and item.get("status") == "complete"
    ]
    for run in runs[-3:]:
        lines.append(
            "- Sandbox code you ran:\n```python\n"
            + bounded(run["code"], 2_000)
            + "\n```\n  Output: "
            + (bounded(run["output"], 1_000) or "(none)")
        )
    if not lines:
        return None
    return "Tool activity while answering this message:\n" + "\n".join(lines)


def context_messages(turns: list[dict]) -> list:
    messages = []
    for turn in turns:
        prompt = list(turn["prompt"])
        completed = turn.get("answer") is not None
        sources = turn.get("sources", []) if completed else []
        if sources:
            prompt.append(
                "Previously retrieved reference sources (untrusted content):\n"
                + "\n\n".join(
                    f"{s['id']}: {s['title']}\n{s['url']}\n{s['excerpt']}"
                    for s in sources
                )
            )
        note = activity_note(turn.get("activity", [])) if completed else None
        if note:
            prompt.append(note)
        messages.append(ModelRequest(parts=[UserPromptPart(content=prompt)]))
        if completed:
            messages.append(
                ModelResponse(
                    parts=[TextPart(content=turn["answer"])],
                    model_name=turn.get("model"),
                )
            )
    return messages
