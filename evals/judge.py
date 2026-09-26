"""LLM judge: scores a free-text answer against a written rubric."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from pydantic_evals.evaluators.llm_as_a_judge import judge_input_output

from vikram.logging import get_logger

logger = get_logger(__name__)

JUDGE_PROVIDER_ENV = "VIKRAM_EVAL_JUDGE_PROVIDER"
JUDGE_MODEL_ENV = "VIKRAM_EVAL_JUDGE_MODEL"
# The judge defaults to this spec's model: a general assistant, not the coder.
DEFAULT_JUDGE_AGENT = "vikram"


@dataclass(frozen=True)
class JudgeModel:
    raw: Any  # pydantic_ai Model
    provider: str | None
    model: str | None

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass(frozen=True)
class Verdict:
    score: float
    passed: bool


def build_judge_model(settings: Any) -> JudgeModel:
    """Resolve the judge model: env override, else the vikram spec's model."""
    from vikram.settings import build_model
    from vikram.specstore import load_agent

    provider = os.environ.get(JUDGE_PROVIDER_ENV)
    model = os.environ.get(JUDGE_MODEL_ENV)
    if not (provider and model):
        spec = load_agent(DEFAULT_JUDGE_AGENT, settings)
        provider = provider or spec.model_provider
        model = model or spec.model
    judge_settings = settings.model_copy(
        update={"model_provider": provider, "model": model}
    )
    # Deterministic grading: a judge that samples creatively adds noise to
    # every before/after comparison.
    built = build_model(
        judge_settings, model_settings={"temperature": 0.0}, agent_name="eval-judge"
    )
    return JudgeModel(raw=built.raw, provider=provider, model=model)


async def judge(
    *, prompt: str, output: str, rubric: str, threshold: float, model: JudgeModel
) -> Verdict:
    grading = await judge_input_output(
        prompt, output, rubric, model.raw, model_settings={"temperature": 0.0}
    )
    score = max(0.0, min(1.0, float(grading.score)))
    logger.info(
        "eval_judge_scored", score=score, reason_length=len(grading.reason or "")
    )
    return Verdict(score=score, passed=score >= threshold)
