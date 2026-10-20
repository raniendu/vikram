"""Questions and plan reviews answered by a person; each interface supplies one."""

import re
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai_harness.ask_user import (
    AskUserAnswer,
    AskUserRequest,
    AskUserResponse,
    Question,
    check_response,
)

__all__ = [
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


class Interaction(Protocol):
    """Puts a question or a plan in front of the person and waits for the answer.

    Adapters own presentation only. Validation, limits, and what the model is told
    stay in the shared toolkit so every interface behaves the same.
    """

    async def ask(self, request: AskUserRequest, /) -> AskUserResponse: ...

    async def review_plan(self, request: PlanRequest, /) -> PlanReview: ...


class Unattended:
    """Nobody can answer: decline questions and leave plans unapproved."""

    async def ask(self, request: AskUserRequest, /) -> AskUserResponse:
        return AskUserResponse(cancelled=True)

    async def review_plan(self, request: PlanRequest, /) -> PlanReview:
        return PlanReview(decision="dismiss")


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
