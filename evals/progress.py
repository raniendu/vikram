"""Progress of a running eval job, for people watching it.

Two outputs, both free of prompts and model output (case ids and counts only):

- one plain-text line per step on stderr, which ``enqueue --wait`` shows in the
  terminal and a background worker appends to ``worker.log``;
- ``.vikram/evals/progress.json``, rewritten at each step, which
  ``python -m evals status`` reads to show where a job is and how long is left.

The worker owns the job-level fields. Each suite runs in a subprocess (a git
worktree of the commit under test) that finds the file through
``PROGRESS_ENV`` and updates only the ``suite`` entry. The worker is blocked
waiting on that subprocess, so the two never write at the same time.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

PROGRESS_ENV = "VIKRAM_EVALS_PROGRESS"
PROGRESS_FILE = "progress.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def format_duration(seconds: float) -> str:
    """``42s``, ``7m 05s``, ``1h 03m``."""
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def read(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def say(line: str, stream: TextIO | None = None) -> None:
    """One human-readable progress line, stamped with local time."""
    stamp = datetime.now().astimezone().strftime("%H:%M:%S")
    out = stream or sys.stderr
    out.write(f"[evals {stamp}] {line}\n")
    out.flush()


class JobProgress:
    """Job-level progress, written by the worker."""

    def __init__(self, path: Path, *, sha: str, agents: list[str], repeats: int):
        self.path = path
        self.data: dict[str, Any] = {
            "pid": os.getpid(),
            "sha": sha,
            "agents": agents,
            "repeats": repeats,
            "started_at": _now_iso(),
            "step": None,
            "suite": None,
        }
        self._started = time.monotonic()
        _write(self.path, self.data)

    def step(self, index: int, total: int, *, agent: str, side: str, sha: str) -> None:
        """A suite run is starting. ``side`` is ``before`` or ``after``."""
        self.data["step"] = {
            "index": index,
            "total": total,
            "agent": agent,
            "side": side,
            "sha": sha,
        }
        self.data["suite"] = None
        _write(self.path, self.data)
        say(
            f"step {index}/{total}: {agent}, {side} ({sha[:7]}). "
            "Preparing the commit's Python environment; the first run for a "
            "commit can take a few minutes."
        )

    def finish(self, outcome: str) -> None:
        say(f"job {self.data['sha'][:7]} {outcome} after {self.elapsed()}")
        self.path.unlink(missing_ok=True)

    def elapsed(self) -> str:
        return format_duration(time.monotonic() - self._started)


class SuiteProgress:
    """Case-by-case progress inside one suite run.

    ``path`` is None for a standalone ``python -m evals run``: lines are still
    printed, nothing is written.
    """

    def __init__(
        self,
        agent: str,
        *,
        cases: int,
        repeats: int,
        path: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.agent = agent
        self.cases = cases
        self.repeats = repeats
        self.total = cases * repeats
        self.path = path
        self._clock = clock
        self._started = clock()
        self._run_seconds = 0.0
        self._run_began = self._started
        self.done = 0
        self.passed = 0
        self.skipped = 0
        say(f"{agent}: {cases} cases x {repeats} repeats = {self.total} agent runs")
        self._save(case_id=None, case_index=0, repeat=0)

    def eta_seconds(self) -> float | None:
        """Mean time per finished run x runs left; None before the first."""
        if self.done == 0:
            return None
        remaining = self.total - self.done - self.skipped * self.repeats
        return max(0.0, self._run_seconds / self.done * remaining)

    def note(self, line: str) -> None:
        """A free-form progress line (e.g. a retry), stamped like the rest."""
        say(line)

    def skip(self, case_id: str, case_index: int, reason: str) -> None:
        self.skipped += 1
        say(f"case {case_index}/{self.cases} {case_id}: skipped ({reason})")
        self._save(case_id=case_id, case_index=case_index, repeat=0)

    def run_started(self, case_id: str, case_index: int, repeat: int) -> None:
        self._run_began = self._clock()
        say(
            f"case {case_index}/{self.cases} {case_id}, repeat "
            f"{repeat}/{self.repeats}: running ({self._counts()})"
        )
        self._save(case_id=case_id, case_index=case_index, repeat=repeat)

    def run_finished(
        self,
        case_id: str,
        case_index: int,
        repeat: int,
        *,
        passed: bool,
        judge_score: float | None,
        error_type: str | None,
    ) -> None:
        took = self._clock() - self._run_began
        self._run_seconds += took
        self.done += 1
        self.passed += passed
        verdict = "passed" if passed else "failed"
        extras = [format_duration(took)]
        if judge_score is not None:
            extras.append(f"judge {judge_score:.2f}")
        if error_type:
            extras.append(f"error {error_type}")
        say(
            f"case {case_index}/{self.cases} {case_id}, repeat "
            f"{repeat}/{self.repeats}: {verdict} ({', '.join(extras)}). "
            f"{self._counts()}"
        )
        self._save(case_id=case_id, case_index=case_index, repeat=repeat)

    def finish(self) -> None:
        rate = f"{self.passed / self.done:.0%}" if self.done else "n/a"
        say(
            f"{self.agent}: suite finished in "
            f"{format_duration(self._clock() - self._started)}, "
            f"{self.passed}/{self.done} runs passed ({rate})"
        )

    def _counts(self) -> str:
        text = f"{self.done}/{self.total} runs done"
        eta = self.eta_seconds()
        if eta is not None:
            text += f", about {format_duration(eta)} left"
        return text

    def _save(self, *, case_id: str | None, case_index: int, repeat: int) -> None:
        if self.path is None:
            return
        data = read(self.path)
        if data is None:  # the worker's file is gone: the job ended
            return
        eta = self.eta_seconds()
        data["suite"] = {
            "agent": self.agent,
            "cases": self.cases,
            "repeats": self.repeats,
            "case_index": case_index,
            "case_id": case_id,
            "repeat": repeat,
            "runs_total": self.total,
            "runs_done": self.done,
            "runs_passed": self.passed,
            "cases_skipped": self.skipped,
            "eta_seconds": None if eta is None else round(eta),
            "updated_at": _now_iso(),
        }
        _write(self.path, data)

    @classmethod
    def from_env(cls, agent: str, *, cases: int, repeats: int) -> SuiteProgress:
        raw = os.environ.get(PROGRESS_ENV)
        return cls(agent, cases=cases, repeats=repeats, path=Path(raw) if raw else None)


def _alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def describe(path: Path, *, now: datetime | None = None) -> list[str]:
    """Lines for ``status``: what the running job is doing, or nothing."""
    data = read(path)
    if data is None:
        return ["running: nothing"]
    if not _alive(data.get("pid")):
        return [
            f"running: nothing (job {str(data.get('sha', ''))[:7]} stopped "
            "without finishing; see .vikram/evals/logs/worker.log)"
        ]
    now = now or datetime.now(timezone.utc)
    try:
        elapsed = (now - datetime.fromisoformat(data["started_at"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        elapsed = 0.0
    lines = [
        f"running: job {data['sha'][:7]} for {', '.join(data['agents'])}, "
        f"{data['repeats']} repeats, {format_duration(elapsed)} so far"
    ]
    step = data.get("step")
    if step:
        lines.append(
            f"  step {step['index']}/{step['total']}: {step['agent']}, "
            f"{step['side']} ({step['sha'][:7]})"
        )
    suite = data.get("suite")
    if step and suite is None:
        lines.append("  preparing the commit's Python environment")
    elif step:
        where = (
            f"case {suite['case_index']}/{suite['cases']} {suite['case_id']}, "
            f"repeat {suite['repeat']}/{suite['repeats']}"
            if suite["case_id"]
            else "starting"
        )
        runs = (
            f"{suite['runs_done']}/{suite['runs_total']} runs done, "
            f"{suite['runs_passed']} passed"
        )
        if suite.get("eta_seconds") is not None:
            runs += f", about {format_duration(suite['eta_seconds'])} left"
            if step["index"] < step["total"]:
                runs += " in this step"
        lines += [f"  {where}", f"  {runs}"]
    return lines
