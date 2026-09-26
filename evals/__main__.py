"""``python -m evals`` command line.

Commands people run:
  status        running job's progress and time left, queue, last results
  detect        dry run: what a commit changed and whether it would trigger
  compare A B   before/after table (run ids, commit shas, labels or files)
  report        write the HTML trend page
  run           run the suite on the working tree now (not committed)
  enqueue       queue a before/after run for HEAD by hand
  record        commit history files left uncommitted
  check-models  re-run agents whose Ollama model was re-pulled

Commands git and the worker run:
  hook          post-commit entry point
  worker        drain the job queue
  run-suite     run one agent's suite in the current checkout
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from evals import STATE_RELPATH
from vikram.logging import configure_logging, get_logger

logger = get_logger(__name__)

LOG_LEVEL_ENV = "VIKRAM_EVALS_LOG_LEVEL"


def _repo() -> Path:
    from evals.gitutil import repo_root

    return repo_root()


def _cmd_hook(args: argparse.Namespace) -> int:
    from evals.orchestrate import hook, log_path

    try:
        job = hook(_repo())
    except Exception:
        # Never fail a commit because of evals.
        logger.exception("eval_hook_failed")
        return 0
    if job is not None:
        sys.stderr.write(
            f"vikram-evals: queued {', '.join(job.agents)} for {job.sha[:7]} "
            f"(log: {log_path(_repo()).relative_to(_repo())})\n"
        )
    return 0


def _cmd_detect(args: argparse.Namespace) -> int:
    from evals import gitutil
    from evals.cases import list_agents
    from evals.changes import detect_changes

    repo = _repo()
    base = gitutil.rev_parse(repo, args.base)
    head = gitutil.rev_parse(repo, args.head)
    if base is None or head is None:
        sys.stderr.write(
            f"Unknown commit: {args.base if base is None else args.head}\n"
        )
        return 1
    changeset = detect_changes(repo, base, head, list_agents())
    sys.stdout.write(f"{base[:7]}..{head[:7]}\n")
    agents = changeset.agents()
    if not agents:
        sys.stdout.write("no eval-relevant changes; the hook would not queue a run\n")
        return 0
    for agent in agents:
        described = changeset.for_agent(agent)
        sys.stdout.write(f"{agent}: {', '.join(described['kinds'])}\n")
        for path in described["files"]:
            sys.stdout.write(f"  file  {path}\n")
        for key, value in described["details"].items():
            sys.stdout.write(f"  {key}: {json.dumps(value)}\n")
    sys.stdout.write(f"the hook would queue: {', '.join(agents)}\n")
    return 0


def _cmd_worker(args: argparse.Namespace) -> int:
    from evals.orchestrate import worker

    worker(_repo(), repeats=args.repeats)
    return 0


def _cmd_run_suite(args: argparse.Namespace) -> int:
    from evals.orchestrate import EXIT_MODEL_UNAVAILABLE
    from evals.runner import ModelUnavailableError, run_suite

    try:
        result = asyncio.run(
            run_suite(
                args.agent,
                repeats=args.repeats,
                case_filter=args.case or None,
                details_dir=Path(args.details) if args.details else None,
            )
        )
    except ModelUnavailableError:
        logger.warning("eval_model_unavailable", agent=args.agent)
        return EXIT_MODEL_UNAVAILABLE
    Path(args.out).write_text(json.dumps(result, indent=2))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    from evals.cases import list_agents
    from evals.runner import ModelUnavailableError, run_suite

    repo = _repo()
    agents = args.agent or list_agents()
    for agent in agents:
        label = f"{args.label}-{agent}" if len(agents) > 1 else args.label
        run_dir = repo / STATE_RELPATH / "runs" / f"manual-{label}"
        try:
            result = asyncio.run(
                run_suite(
                    agent,
                    repeats=args.repeats,
                    case_filter=args.case or None,
                    details_dir=run_dir / "cases",
                )
            )
        except ModelUnavailableError:
            sys.stderr.write(f"{agent}: model server not reachable; nothing run.\n")
            return 1
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "result.json").write_text(json.dumps(result, indent=2))
        s = result["summary"]
        rate = "n/a" if s["pass_rate"] is None else f"{s['pass_rate'] * 100:.0f}%"
        sys.stdout.write(f"{agent}: pass {rate} over {s['cases']} cases -> {label}\n")
    return 0


def _cmd_enqueue(args: argparse.Namespace) -> int:
    from evals import gitutil
    from evals.cases import list_agents
    from evals.orchestrate import Job, enqueue, spawn_worker, worker

    repo = _repo()
    sha = gitutil.rev_parse(repo, args.sha)
    if sha is None:
        sys.stderr.write(f"Unknown commit {args.sha!r}.\n")
        return 1
    job = Job(
        sha=sha,
        branch=gitutil.current_branch(repo),
        agents=args.agent or list_agents(),
        trigger="manual",
    )
    enqueue(repo, job)
    if args.wait:
        worker(repo, repeats=args.repeats)
    else:
        spawn_worker(repo)
    return 0


def _cmd_record(args: argparse.Namespace) -> int:
    from evals.orchestrate import record_uncommitted

    committed = record_uncommitted(_repo())
    sys.stdout.write("committed\n" if committed else "nothing to commit\n")
    return 0


def _cmd_check_models(args: argparse.Namespace) -> int:
    from evals.orchestrate import check_models

    job = check_models(_repo())
    msg = f"queued {', '.join(job.agents)}\n" if job else "no model changes\n"
    sys.stdout.write(msg)
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    from evals.compare import ResultNotFound, render, resolve_result

    repo = _repo()
    try:
        before = resolve_result(repo, args.before, args.agent)
        after = resolve_result(repo, args.after, args.agent)
    except ResultNotFound as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    sys.stdout.write(render(before, after))
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    from evals.report import write_report

    repo = _repo()
    out = Path(args.out) if args.out else repo / STATE_RELPATH / "report.html"
    write_report(repo, out)
    sys.stdout.write(f"{out}\n")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from evals.orchestrate import progress_path, state_dir, summary_log_path
    from evals.progress import describe

    repo = _repo()
    pending = state_dir(repo) / "pending.json"
    if pending.exists():
        job = json.loads(pending.read_text())
        sys.stdout.write(
            f"queued: {', '.join(job['agents'])} @ {job['sha'][:7]} ({job['trigger']})\n"
        )
    else:
        sys.stdout.write("queued: nothing\n")
    sys.stdout.write("\n".join(describe(progress_path(repo))) + "\n")
    summary = summary_log_path(repo)
    if summary.exists():
        lines = summary.read_text().splitlines()[-args.lines :]
        sys.stdout.write("recent:\n" + "\n".join(f"  {line}" for line in lines) + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("hook").set_defaults(fn=_cmd_hook)

    p = sub.add_parser("detect", help="dry run of the hook's change detection")
    p.add_argument("--base", default="HEAD~1")
    p.add_argument("--head", default="HEAD")
    p.set_defaults(fn=_cmd_detect)

    p = sub.add_parser("worker")
    p.add_argument("--repeats", type=int)
    p.set_defaults(fn=_cmd_worker)

    p = sub.add_parser("run-suite")
    p.add_argument("--agent", required=True)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--case", action="append")
    p.add_argument("--out", required=True)
    p.add_argument("--details")
    p.set_defaults(fn=_cmd_run_suite)

    p = sub.add_parser("run")
    p.add_argument("--agent", action="append")
    p.add_argument("--label", required=True)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--case", action="append")
    p.set_defaults(fn=_cmd_run)

    p = sub.add_parser("enqueue")
    p.add_argument("--sha", default="HEAD")
    p.add_argument("--agent", action="append")
    p.add_argument("--repeats", type=int)
    p.add_argument("--wait", action="store_true", help="run in the foreground")
    p.set_defaults(fn=_cmd_enqueue)

    sub.add_parser("record").set_defaults(fn=_cmd_record)
    sub.add_parser("check-models").set_defaults(fn=_cmd_check_models)

    p = sub.add_parser("compare")
    p.add_argument("before")
    p.add_argument("after")
    p.add_argument("--agent")
    p.set_defaults(fn=_cmd_compare)

    p = sub.add_parser("report")
    p.add_argument("--out")
    p.set_defaults(fn=_cmd_report)

    p = sub.add_parser("status")
    p.add_argument("--lines", type=int, default=10)
    p.set_defaults(fn=_cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # The hook runs on every commit; keep the terminal quiet unless it breaks.
    # A suite run prints its own progress lines (evals/progress.py); the agent
    # under test's INFO logs would bury them, so they show only on request.
    level = "WARNING" if args.command in {"hook", "run-suite", "run"} else "INFO"
    configure_logging(os.environ.get(LOG_LEVEL_ENV, level), stream=sys.stderr)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
