"""Generate every configuration first, then grade with one local judge."""

import asyncio
import inspect
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import LLMJudge
from pydantic_evals.reporting import EvaluationReport

from vikram.agent import SYSTEM_PROMPT
from vikram.evals.cases import JUDGE_POLICY, CaseSpec, ObjectiveCheck, _same_json
from vikram.evals.history import CaseResult, RunConfig, RunRecord, fingerprint
from vikram.evals.provenance import capture_revision
from vikram.evals.thinking import thinking_label, thinking_request


@dataclass
class _Answer:
    spec: CaseSpec = field(repr=False)
    counts: CaseResult
    attempt: int
    output: str = field(repr=False)


@dataclass
class _PendingRun:
    record: RunRecord
    answers: list[_Answer] = field(default_factory=list, repr=False)
    saved: bool = False


def _new_run(
    config: RunConfig, cases: Sequence[CaseSpec], label: str, experiment: str
) -> _PendingRun:
    now = datetime.now(UTC)
    return _PendingRun(
        RunRecord(
            run_id=f"{now:%Y%m%dT%H%M%S%fZ}-{uuid4().hex[:8]}",
            started_at=now,
            duration_seconds=0,
            config=config,
            label=label,
            experiment=experiment,
            revision=capture_revision(),
            benchmark_version=2,
            suite_hash=fingerprint([case.model_dump() for case in cases]),
            agent_hash=fingerprint(SYSTEM_PROMPT),
            protocol_hash=fingerprint(
                {
                    "scoring_version": 1,
                    "objective_check": inspect.getsource(ObjectiveCheck),
                    "json_check": inspect.getsource(_same_json),
                    "judge_policy": JUDGE_POLICY,
                    "judge_output": "LLMJudge: all rubric criteria, binary assertion",
                    "judge_max_tokens": 512,
                }
            ),
            cases=[
                CaseResult(
                    id=case.id,
                    label=case.label,
                    group=case.group,
                    samples=config.samples,
                )
                for case in cases
            ],
        )
    )


def _count_grade(
    report: EvaluationReport[str, str, None],
    counts: CaseResult,
    progress: Callable[[str], None],
) -> None:
    if (
        report.failures
        or report.cases[0].evaluator_failures
        or not report.cases[0].assertions
    ):
        counts.grading_errors += 1
        progress("  unscored: evaluator failed")
    elif all(assertion.value for assertion in report.cases[0].assertions.values()):
        counts.passed += 1
        progress("  pass")
    else:
        counts.failed += 1
        progress("  fail")


async def _generate(
    pending: _PendingRun,
    cases: Sequence[CaseSpec],
    agent: Agent[None, str],
    progress: Callable[[str], None],
) -> None:
    run = pending.record
    config = run.config
    started = time.monotonic()

    async def answer(prompt: str) -> str:
        async with asyncio.timeout(config.timeout_seconds):
            result = await agent.run(
                prompt,
                message_history=[],
                model_settings={
                    "temperature": config.temperature,
                    "extra_body": thinking_request(config.thinking),
                },
            )
        return result.output

    try:
        async with agent:
            for spec, counts in zip(cases, run.cases):
                dataset: Dataset[str, str, None] = Dataset(
                    name="vikram-starter",
                    cases=[
                        Case[str, str, None](
                            name=spec.id,
                            inputs=spec.prompt,
                            evaluators=(
                                ()
                                if spec.check == "rubric"
                                else (
                                    ObjectiveCheck(
                                        check=spec.check, expected=spec.expected
                                    ),
                                )
                            ),
                        )
                    ],
                )
                for attempt in range(1, config.samples + 1):
                    progress(f"Generate {spec.id}: attempt {attempt}/{config.samples}")
                    report = await dataset.evaluate(
                        answer, max_concurrency=1, progress=False
                    )
                    if report.failures:
                        counts.generation_errors += 1
                        progress("  unscored: agent request failed")
                    elif spec.check == "rubric":
                        pending.answers.append(
                            _Answer(spec, counts, attempt, report.cases[0].output)
                        )
                        progress("  answer ready for judging")
                    else:
                        _count_grade(report, counts, progress)
                    # Reports contain answers and error bodies. Never save or print them.
                    del report
    finally:
        run.duration_seconds += time.monotonic() - started
        if capture_revision() != run.revision:
            progress(
                "Agent files or dependencies changed during generation; provenance is uncertain."
            )
            run.revision = None


async def _grade(
    pending: _PendingRun, judge: Model, progress: Callable[[str], None]
) -> None:
    config = pending.record.config
    started = time.monotonic()
    try:
        for answer in pending.answers:
            evaluator = LLMJudge(
                rubric=JUDGE_POLICY + answer.spec.rubric,
                model=judge,
                include_input=True,
                model_settings={
                    "temperature": config.judge_temperature,
                    "timeout": config.judge_timeout_seconds,
                    "max_tokens": 512,
                    "extra_body": thinking_request(config.judge_thinking),
                },
                assertion={"evaluation_name": "passed", "include_reason": False},
                score=False,
            )
            dataset: Dataset[str, str, None] = Dataset(
                name="vikram-grading",
                cases=[
                    Case[str, str, None](
                        name=answer.spec.id,
                        inputs=answer.spec.prompt,
                        evaluators=(evaluator,),
                    )
                ],
            )

            async def cached_answer(_: str) -> str:
                return answer.output

            progress(
                f"Judge {answer.spec.id}: attempt {answer.attempt}/{config.samples}"
            )
            try:
                async with asyncio.timeout(config.judge_timeout_seconds):
                    report = await dataset.evaluate(
                        cached_answer, max_concurrency=1, progress=False
                    )
                _count_grade(report, answer.counts, progress)
                del report
            except TimeoutError:
                answer.counts.grading_errors += 1
                progress("  unscored: evaluator timed out")
            finally:
                answer.output = ""
    finally:
        # Exclude time spent generating or grading other configurations.
        pending.record.duration_seconds += time.monotonic() - started
        pending.answers.clear()


async def evaluate_many(
    configs: Sequence[RunConfig],
    cases: Sequence[CaseSpec],
    agent_factory: Callable[[str], Agent[None, str]],
    judge: Model | None,
    progress: Callable[[str], None],
    on_result: Callable[[RunRecord], None],
    *,
    label: str = "",
    experiment: str = "",
) -> list[RunRecord]:
    """Keep rubric answers in memory until every model has finished generation."""
    if judge is None and any(case.check == "rubric" for case in cases):
        raise ValueError("Judged cases require an explicit judge model.")
    pending_runs: list[_PendingRun] = []

    def finish(pending: _PendingRun) -> None:
        pending.saved = True
        on_result(pending.record)

    try:
        progress(
            "Phase 1/2: generate all models and thinking levels; objective checks run locally."
        )
        for config in configs:
            progress(f"{config.model} · thinking {thinking_label(config.thinking)}")
            pending = _new_run(config, cases, label, experiment)
            pending_runs.append(pending)
            await _generate(pending, cases, agent_factory(config.model), progress)
            if not pending.answers:
                finish(pending)
        if judge is not None:
            progress(
                "Phase 2/2: judge all queued answers; no further agent generation."
            )
            for pending in pending_runs:
                if not pending.saved:
                    progress(
                        f"Grading {pending.record.config.model} · thinking {thinking_label(pending.record.config.thinking)}"
                    )
                    await _grade(pending, judge, progress)
                    finish(pending)
    except asyncio.CancelledError:
        for pending in pending_runs:
            if not pending.saved:
                pending.record.interrupted = True
        progress(
            "Interrupted; saving outcome counts. Unjudged answers remain unscored."
        )
    finally:
        for pending in pending_runs:
            pending.answers.clear()
            if not pending.saved:
                finish(pending)
    return [pending.record for pending in pending_runs]


async def evaluate(
    config: RunConfig,
    cases: Sequence[CaseSpec],
    agent: Agent[None, str],
    judge: Model | None,
    progress: Callable[[str], None],
    *,
    label: str = "",
    experiment: str = "",
) -> RunRecord:
    """Evaluate one configuration using the same two-phase runner as a sweep."""
    runs = await evaluate_many(
        [config],
        cases,
        lambda _: agent,
        judge,
        progress,
        lambda _: None,
        label=label,
        experiment=experiment,
    )
    return runs[0]
