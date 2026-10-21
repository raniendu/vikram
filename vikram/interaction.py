"""Questions, plan reviews, and browser approvals answered by a person.

Each interface supplies one `Interaction`; interfaces that can show a browser
action to the person also implement `Approver`.
"""

import re
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai_harness.ask_user import (
    AskUserAnswer,
    AskUserRequest,
    AskUserResponse,
    Question,
    check_response,
)

__all__ = [
    "ActionRequest",
    "Approver",
    "AskUserAnswer",
    "AskUserRequest",
    "AskUserResponse",
    "Interaction",
    "PlanRequest",
    "PlanReview",
    "Question",
    "ScriptedInteraction",
    "Unattended",
    "answer_from_text",
    "check_response",
]


def answer_from_text(question: Question, text: str) -> AskUserAnswer | None:
    """Read a typed reply: option numbers pick options, other text is the answer.

    Several comma-separated numbers are allowed for multi-select questions.
    Returns `None` for numbers that do not identify valid options.
    """
    text = " ".join(text.split())
    parts = [part.strip() for part in text.split(",")]
    if text and all(part.isdigit() for part in parts):
        picks = [int(part) for part in parts]
        if (
            all(1 <= pick <= len(question.options) for pick in picks)
            and len(set(picks)) == len(picks)
            and (question.multi_select or len(picks) == 1)
        ):
            return AskUserAnswer(
                header=question.header,
                selected=tuple(question.options[pick - 1].label for pick in picks),
            )
        return None
    return AskUserAnswer(header=question.header, custom_answer=text) if text else None


@dataclass(frozen=True, kw_only=True)
class PlanRequest:
    """A submitted plan; `id` is the tool call ID, so a UI can match its reply."""

    id: str
    plan: str


class PlanReview(BaseModel):
    """A person's decision about a submitted plan."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)
    decision: Literal["approve", "revise", "dismiss"]
    feedback: str | None = Field(default=None, max_length=2000)


@dataclass(frozen=True, kw_only=True)
class ActionRequest:
    """A browser action that waits for the person's approval before it runs.

    `id` is the tool call ID, so a UI can match its reply. `summary` says what
    would happen ("Click “Place order” on shop.example") and `reason` why it
    needs approval ("It looks like it buys or pays for something.").
    """

    id: str
    summary: str
    reason: str
    url: str


class Interaction(Protocol):
    """Puts a question or a plan in front of the person and waits for the answer.

    Adapters own presentation only. Validation, limits, and what the model is told
    stay in the shared toolkit so every interface behaves the same.
    """

    async def ask(self, request: AskUserRequest, /) -> AskUserResponse: ...

    async def review_plan(self, request: PlanRequest, /) -> PlanReview: ...


@runtime_checkable
class Approver(Protocol):
    """An interaction that can ask the person to allow one browser action.

    Without it, every action that needs approval is declined: evaluations, piped
    runs, and tests never approve a form submission, sign-in, or purchase.
    """

    async def approve(self, request: ActionRequest, /) -> bool: ...


class Unattended:
    """Nobody can answer: decline questions, plans, and browser actions."""

    async def ask(self, request: AskUserRequest, /) -> AskUserResponse:
        return AskUserResponse(cancelled=True)

    async def review_plan(self, request: PlanRequest, /) -> PlanReview:
        return PlanReview(decision="dismiss")

    async def approve(self, request: ActionRequest, /) -> bool:
        return False


class ScriptedInteraction:
    """A deterministic stand-in for a person, for evaluations and tests.

    Each question gets the first option whose label or description matches
    `pattern`, otherwise `answer` typed in as the user's own words. With neither,
    questions are declined. Every submitted plan gets `plan_decision`.
    """

    def __init__(
        self,
        answer: str | None = None,
        *,
        pattern: str | None = None,
        plan_decision: Literal["approve", "revise", "dismiss"] = "approve",
    ):
        self.answer = answer
        self.pattern = re.compile(pattern, re.IGNORECASE) if pattern else None
        self.plan_decision = plan_decision

    async def ask(self, request: AskUserRequest, /) -> AskUserResponse:
        answers = []
        for question in request.questions:
            match = next(
                (
                    option.label
                    for option in question.options
                    if self.pattern
                    and self.pattern.search(
                        f"{option.label}\n{option.description or ''}"
                    )
                ),
                None,
            )
            if match is not None:
                answers.append(AskUserAnswer(header=question.header, selected=(match,)))
            elif self.answer is not None:
                answers.append(
                    AskUserAnswer(header=question.header, custom_answer=self.answer)
                )
            else:
                return AskUserResponse(cancelled=True)
        return AskUserResponse(answers=tuple(answers))

    async def review_plan(self, request: PlanRequest, /) -> PlanReview:
        return PlanReview(decision=self.plan_decision)
