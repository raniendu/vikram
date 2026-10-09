"""Manual evaluation commands, separate from the chat interface."""

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError
from pydantic_ai.models import Model
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.providers.ollama import OllamaProvider

from vikram.agent import DEFAULT_MODEL, build_agent
from vikram.evals.cases import CASES, CaseSpec
from vikram.evals.history import (
    DEFAULT_DIRECTORY,
    DEFAULT_JUDGE,
    RunConfig,
    RunRecord,
    save_run,
)
from vikram.evals.report import write_report
from vikram.evals.runner import evaluate_many
from vikram.evals.thinking import discover_thinking, select_levels, thinking_label


def _progress(message: str) -> None:
    sys.stderr.write(message + "\n")


async def _benchmark(
    configs: list[RunConfig],
    cases: list[CaseSpec],
    judge: Model | None,
    directory: Path,
    label: str,
    experiment: str,
) -> int:
    def completed(run: RunRecord) -> None:
        config = run.config
        history_path = save_run(run, directory)
        report_path = write_report(directory)
        scores = run.scores()
        summary = (
            ", ".join(f"pass@{k}: {value:.1%}" for k, value in scores.items())
            if scores is not None
            else "Combined score unavailable: run has unscored attempts."
        )
        sys.stdout.write(
            f"{config.model} · thinking {thinking_label(config.thinking)}\n"
            f"{run.status.capitalize()}. {summary}\n"
            f"History: {history_path.resolve()}\nReport: {report_path.resolve()}\n"
        )

    runs = await evaluate_many(
        configs,
        cases,
        build_agent,
        judge,
        _progress,
        completed,
        label=label,
        experiment=experiment,
    )
    return (
        130
        if any(run.interrupted for run in runs)
        else int(any(run.status != "complete" for run in runs))
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run an evaluation or rebuild its offline report."""
    parser = argparse.ArgumentParser(
        description="Evaluate Vikram and view local history."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="run cases and save metrics + HTML")
    run_parser.add_argument(
        "--model", action="append", help="local model; repeat to benchmark several"
    )
    run_parser.add_argument(
        "--thinking",
        nargs="+",
        default=["all"],
        help="all supported levels (default), on/off, named levels, or default for an unswept run",
    )
    run_parser.add_argument(
        "--judge-model",
        default=DEFAULT_JUDGE,
        help="local Ollama judge (default: %(default)s)",
    )
    run_parser.add_argument(
        "--judge-thinking",
        default="off",
        help="one fixed judge level: off (default), on, a named level, or default",
    )
    run_parser.add_argument(
        "--label", default="", help="short description of this agent version/change"
    )
    run_parser.add_argument(
        "--experiment",
        default="",
        help="group runs for a model, prompt, tool, or harness comparison",
    )
    run_parser.add_argument(
        "--samples", type=int, default=5, help="attempts per case (default: 5)"
    )
    run_parser.add_argument(
        "--k",
        type=int,
        nargs="+",
        default=[1, 3, 5],
        help="attempt budgets (default: 1 3 5)",
    )
    run_parser.add_argument(
        "--case",
        action="append",
        choices=[c.id for c in CASES],
        help="select a case; repeat to select several",
    )
    report_parser = commands.add_parser(
        "report", help="rebuild HTML from saved metrics without model calls"
    )
    for command in (run_parser, report_parser):
        command.add_argument(
            "--directory",
            type=Path,
            default=DEFAULT_DIRECTORY,
            help="artifact directory (default: %(default)s)",
        )
    args = parser.parse_args(argv)
    try:
        if args.command == "report":
            path = write_report(args.directory)
            sys.stdout.write(f"Report: {path.resolve()}\n")
            return 0
        selected = [case for case in CASES if args.case is None or case.id in args.case]
        needs_judge = any(case.group == "judged" for case in selected)
        models = list(
            dict.fromkeys(
                model.strip()
                for model in (
                    args.model
                    or [os.getenv("OLLAMA_MODEL", "").strip() or DEFAULT_MODEL]
                )
            )
        )
        if any(not model for model in models) or not args.judge_model.strip():
            parser.error("Model names must not be empty.")
        try:
            config = RunConfig(
                model=models[0],
                judge_model=args.judge_model.strip() if needs_judge else None,
                judge_provider="ollama" if needs_judge else None,
                judge_timeout_seconds=300.0,
                samples=args.samples,
                ks=args.k,
            )
        except ValidationError:
            parser.error("Require samples >= 1 and every k between 1 and samples.")
        if needs_judge:
            assert config.judge_model is not None
            try:
                judge_levels = select_levels(
                    discover_thinking(config.judge_model), [args.judge_thinking]
                )
            except ValueError as exc:
                sys.stderr.write(f"Judge configuration: {exc}\n")
                return 1
            if len(judge_levels) != 1:
                parser.error("Choose one fixed judge thinking level.")
            config.judge_thinking = judge_levels[0]
        configs = []
        for model in models:
            try:
                controls = discover_thinking(model)
                levels = select_levels(controls, args.thinking)
            except ValueError as exc:
                sys.stderr.write(f"{exc}\n")
                return 1
            configs.extend(
                config.model_copy(
                    update={
                        "model": model,
                        "thinking": level,
                        "thinking_levels": controls.values if controls else None,
                    }
                )
                for level in levels
            )
        _progress(
            f"Benchmark: {len(configs)} model/thinking configurations, "
            f"{len(selected)} cases, {config.samples} attempts per case."
        )
        if needs_judge:
            _progress(
                f"Local judge: {config.judge_model} · thinking {thinking_label(config.judge_thinking)} · temperature {config.judge_temperature:g}"
            )
        judge = (
            OllamaModel(
                config.judge_model,
                provider=OllamaProvider(
                    base_url="http://localhost:11434/v1", api_key="ollama"
                ),
            )
            if config.judge_model is not None
            else None
        )
        return asyncio.run(
            _benchmark(
                configs,
                selected,
                judge,
                args.directory,
                args.label.strip(),
                args.experiment.strip(),
            )
        )
    except KeyboardInterrupt:
        sys.stderr.write("\nEvaluation interrupted.\n")
        return 130
    except (OSError, ValueError) as exc:
        # Do not print exception bodies that may include model responses or keys.
        sys.stderr.write(
            f"Evaluation artifacts could not be processed ({type(exc).__name__}).\n"
        )
        return 1
