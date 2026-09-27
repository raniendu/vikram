"""``pytest --evals``: score the working tree and fail on a regression.

After the offline tests pass, each agent's suite runs against the code as it
is on disk (committed or not) and is compared with the nearest recorded result
on HEAD or an ancestor that used the same suite. A case that got worse beyond
the noise margins (``history.case_status``) fails the session.

Nothing is committed: the result is saved as a manual run
(``.vikram/evals/runs/manual-pytest-<agent>/``) so ``python -m evals compare``
can show it again. Commit-to-commit history stays with the post-commit hook.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals import STATE_RELPATH, gitutil, history, progress
from evals.cases import list_agents
from vikram.logging import get_logger

logger = get_logger(__name__)

LABEL_PREFIX = "pytest"


@dataclass
class AgentOutcome:
    agent: str
    result: dict[str, Any] | None = None
    baseline: dict[str, Any] | None = None
    worse_cases: list[str] = field(default_factory=list)
    unscored_cases: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def failed(self) -> bool:
        # An unscored case (model server busy through every retry) proves
        # nothing either way, so the gate can't pass on it.
        return self.error is not None or bool(self.worse_cases or self.unscored_cases)


@dataclass
class GateResult:
    outcomes: list[AgentOutcome]

    @property
    def failed(self) -> bool:
        return any(outcome.failed for outcome in self.outcomes)

    def report(self) -> str:
        from evals.compare import render

        blocks: list[str] = []
        for outcome in self.outcomes:
            if outcome.error:
                blocks.append(f"{outcome.agent}: FAILED: {outcome.error}")
                continue
            assert outcome.result is not None
            if outcome.baseline is None:
                summary = outcome.result["summary"]
                rate = summary.get("pass_rate")
                shown = "n/a" if rate is None else f"{rate:.0%}"
                line = (
                    f"{outcome.agent}: pass {shown} over {summary['cases']} cases. "
                    "No recorded result to compare with yet (run "
                    "`python -m evals enqueue --wait` on a commit to record one)."
                )
                if outcome.unscored_cases:
                    line += (
                        f"\n{outcome.agent}: FAILED: could not score "
                        f"{', '.join(outcome.unscored_cases)} (model server busy)"
                    )
                blocks.append(line)
                continue
            problems = []
            if outcome.worse_cases:
                problems.append(f"worse on {', '.join(outcome.worse_cases)}")
            if outcome.unscored_cases:
                problems.append(
                    f"could not score {', '.join(outcome.unscored_cases)} "
                    "(model server busy; see `ollama ps` and "
                    "`python -m evals status`)"
                )
            verdict = (
                f"FAILED: {'; '.join(problems)}" if problems else "no case got worse"
            )
            blocks.append(
                f"{render(outcome.baseline, outcome.result)}"
                f"{outcome.agent}: {verdict}"
            )
        return "\n\n".join(blocks)


def run_gate(
    repo: Path,
    *,
    agents: list[str] | None = None,
    repeats: int | None = None,
    runner: Any = None,
) -> GateResult:
    """Score each agent on the working tree against its latest recorded result.

    ``runner`` replaces :func:`evals.runner.run_suite` in tests.
    """
    from evals.orchestrate import _repeats, exclusive_model_use

    if runner is None:
        from evals.runner import run_suite as runner

    head = gitutil.rev_parse(repo, "HEAD")
    records = history.load_records(repo)
    repeats = _repeats(repeats)
    outcomes: list[AgentOutcome] = []
    with exclusive_model_use(
        repo,
        on_wait=lambda: progress.say(
            "pytest --evals: a background eval job is using the model; waiting "
            "for it to finish (`python -m evals status` shows its progress)"
        ),
    ):
        _score_agents(repo, agents, repeats, runner, head, records, outcomes)
    return GateResult(outcomes)


def _score_agents(
    repo: Path,
    agents: list[str] | None,
    repeats: int,
    runner: Any,
    head: str | None,
    records: list[dict[str, Any]],
    outcomes: list[AgentOutcome],
) -> None:
    from evals.runner import ModelUnavailableError

    for agent in agents or list_agents(repo / "evals"):
        outcome = AgentOutcome(agent=agent)
        outcomes.append(outcome)
        run_dir = repo / STATE_RELPATH / "runs" / f"manual-{LABEL_PREFIX}-{agent}"
        progress.say(f"pytest --evals: scoring {agent} on the working tree")
        try:
            result = asyncio.run(
                runner(agent, repeats=repeats, details_dir=run_dir / "cases")
            )
        except ModelUnavailableError:
            outcome.error = "model server not reachable (is Ollama running?)"
            logger.warning("eval_gate_model_unavailable", agent=agent)
            continue
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "result.json").write_text(json.dumps(result, indent=2))
        outcome.result = result
        outcome.unscored_cases = [
            c["id"] for c in result["cases"] if c.get("status") == "unscored"
        ]
        if head is not None:
            outcome.baseline = history.find_baseline(
                repo,
                records,
                agent=agent,
                head=head,
                suite_hash=result["suite_hash"],
                include_head=True,
            )
        if outcome.baseline is not None:
            delta = history.compute_delta(result, outcome.baseline)
            outcome.worse_cases = delta["summary"]["worse_cases"]
        logger.info(
            "eval_gate_scored",
            agent=agent,
            has_baseline=outcome.baseline is not None,
            worse_cases=len(outcome.worse_cases),
            unscored_cases=len(outcome.unscored_cases),
        )
