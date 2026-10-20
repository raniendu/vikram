"""Questions and plan reviews answered by a person; each interface supplies one."""

import re
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai_harness.ask_user import (
    AskUserAnswer,
    AskUserRequest,
    AskUserResponse,
    check_response,
)

__all__ = [
    "AskUserAnswer",
    "AskUserRequest",
    "AskUserResponse",
    "Interaction",
    "PlanReview",
    "ScriptedInteraction",
    "Unattended",
    "check_response",
]


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

    async def review_plan(self, plan: str, /) -> PlanReview: ...


class Unattended:
    """Nobody can answer: decline questions and leave plans unapproved."""

    async def ask(self, request: AskUserRequest, /) -> AskUserResponse:
        return AskUserResponse(cancelled=True)

    async def review_plan(self, plan: str, /) -> PlanReview:
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

    async def review_plan(self, plan: str, /) -> PlanReview:
        return PlanReview(decision=self.plan_decision)
