"""Commit hook, background worker and the before/after pipeline.

Flow for one commit::

    post-commit hook  -> detect changes HEAD~1..HEAD -> queue a job -> spawn worker
    worker            -> for each agent: find or build a baseline, run HEAD,
                         write a history record -> commit evals/history only

Suite runs happen in git worktrees of the exact commit under test, so the
user's working tree (uncommitted edits, branch switches) never leaks into a
result. Both sides of a comparison run the *same* suite: the one at HEAD.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from evals import STATE_RELPATH, changes, gitutil, history, progress
from evals.cases import list_agents, suite_hash
from vikram.logging import get_logger

logger = get_logger(__name__)

AUTOCOMMIT_ENV = "VIKRAM_EVALS_AUTOCOMMIT"
DISABLE_ENV = "VIKRAM_EVALS_DISABLE"
REPEATS_ENV = "VIKRAM_EVALS_REPEATS"
SUITE_TIMEOUT_ENV = "VIKRAM_EVALS_SUITE_TIMEOUT"
# Allowance per repeat for checks (tests_pass is capped at 300s) and the
# judge, and once per run for creating the commit's uv environment.
_CHECKS_ALLOWANCE_SECONDS = 300.0
_SETUP_ALLOWANCE_SECONDS = 600.0
DEFAULT_REPEATS = 3

EXIT_MODEL_UNAVAILABLE = 3

# Env that would make a suite run measure something other than the committed
# specs. Model overrides are also cleared inside the runner.
_SCRUBBED_ENV = ("VIKRAM_SPEC_ROOT", "VIKRAM_MODEL_PROVIDER", "VIKRAM_MODEL")


@dataclass
class Job:
    sha: str
    branch: str | None
    agents: list[str]
    trigger: str = "commit"
    queued_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )


class SuiteRunError(RuntimeError):
    pass


class ModelUnavailable(SuiteRunError):
    pass


# --- state ----------------------------------------------------------------


def state_dir(repo: Path) -> Path:
    path = repo / STATE_RELPATH
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_path(repo: Path) -> Path:
    path = state_dir(repo) / "logs"
    path.mkdir(exist_ok=True)
    return path / "worker.log"


def summary_log_path(repo: Path) -> Path:
    path = state_dir(repo) / "logs"
    path.mkdir(exist_ok=True)
    return path / "summary.log"


def progress_path(repo: Path) -> Path:
    return state_dir(repo) / progress.PROGRESS_FILE


def _pending_path(repo: Path) -> Path:
    return state_dir(repo) / "pending.json"


def enqueue(repo: Path, job: Job) -> Job:
    """Queue ``job``. A newer job replaces a waiting one; agents are merged.

    Coalescing loses nothing: the worker diffs baseline..HEAD, which covers
    every commit in between.
    """
    pending = _pending_path(repo)
    try:
        waiting = json.loads(pending.read_text())
    except (OSError, json.JSONDecodeError):
        waiting = None
    if waiting:
        job.agents = sorted(set(job.agents) | set(waiting.get("agents", [])))
        # A commit run also covers a model rebuild: it records digest changes.
        if waiting.get("trigger") == "commit":
            job.trigger = "commit"
    tmp = pending.with_suffix(f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(asdict(job)))
    os.replace(tmp, pending)
    return job


def _pop_job(repo: Path) -> Job | None:
    pending = _pending_path(repo)
    claimed = pending.with_suffix(".claimed")
    try:
        os.replace(pending, claimed)
    except FileNotFoundError:
        return None
    try:
        return Job(**json.loads(claimed.read_text()))
    except (OSError, json.JSONDecodeError, TypeError):
        logger.exception("eval_job_unreadable")
        return None
    finally:
        claimed.unlink(missing_ok=True)


@contextmanager
def _worker_lock(repo: Path) -> Iterator[bool]:
    handle = open(state_dir(repo) / "worker.lock", "w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


@contextmanager
def exclusive_model_use(repo: Path, on_wait: Any = None) -> Iterator[None]:
    """Hold the worker lock, waiting for a running background job first.

    Two eval runs at once overload a local model server (Ollama answers 503
    "maximum pending requests exceeded"), so a foreground run shares the
    worker's lock. Jobs a commit queued meanwhile are picked up afterwards.
    """
    handle = open(state_dir(repo) / "worker.lock", "w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if on_wait is not None:
                on_wait()
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()
    if _pending_path(repo).exists():
        spawn_worker(repo)


def _append_summary(repo: Path, line: str) -> None:
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    with summary_log_path(repo).open("a") as fh:
        fh.write(f"{stamp} {line}\n")


# --- hook -----------------------------------------------------------------


def hook(repo: Path) -> Job | None:
    """post-commit entry point. Fast: git diff only, never runs a model."""
    if os.environ.get(gitutil.SKIP_ENV) or os.environ.get(DISABLE_ENV) == "1":
        return None
    # post-commit also fires for every commit a rebase or cherry-pick replays;
    # those are not new changes. The next ordinary commit covers the result.
    if gitutil.operation_in_progress(repo):
        return None
    head = gitutil.rev_parse(repo, "HEAD")
    parent = gitutil.parent_of(repo, head) if head else None
    if not head or not parent:
        return None
    agents = list_agents(repo / "evals")
    changeset = changes.detect_changes(repo, parent, head, agents)
    try:
        triggers = changes.trigger_kinds()
    except ValueError:
        logger.exception("eval_triggers_invalid", env=changes.TRIGGERS_ENV)
        return None
    triggered = changeset.agents(triggers)
    if not triggered:
        if changeset.agents():
            logger.info(
                "eval_job_not_triggered",
                sha=head[:7],
                change_kinds=sorted({k for ks in changeset.kinds.values() for k in ks}),
            )
        return None
    job = enqueue(
        repo,
        Job(sha=head, branch=gitutil.current_branch(repo), agents=triggered),
    )
    kinds = sorted({k for a in triggered for k in changeset.kinds[a]})
    logger.info("eval_job_queued", sha=head[:7], agents=job.agents, change_kinds=kinds)
    spawn_worker(repo)
    return job


# Git exports these to hooks, pointing at the hook's own repository state
# (GIT_INDEX_FILE is even relative). Inherited by the worker, they would
# redirect its git worktree/commit calls, so they are dropped.
_HOOK_GIT_ENV = (
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_WORK_TREE",
    "GIT_PREFIX",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


def spawn_worker(repo: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k not in _HOOK_GIT_ENV}
    log = log_path(repo).open("a")
    subprocess.Popen(
        [sys.executable, "-m", "evals", "worker"],
        cwd=repo,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    log.close()


# --- worker ---------------------------------------------------------------


def worker(repo: Path, *, repeats: int | None = None) -> int:
    """Drain the queue. Returns the number of jobs processed."""
    processed = 0
    while True:
        with _worker_lock(repo) as acquired:
            if not acquired:
                logger.info("eval_worker_already_running")
                return processed
            while (job := _pop_job(repo)) is not None:
                try:
                    process_job(repo, job, repeats=repeats)
                except Exception:
                    logger.exception("eval_job_failed", sha=job.sha[:7])
                    _append_summary(repo, f"FAILED job {job.sha[:7]} (see worker.log)")
                processed += 1
        # A hook may have queued a job after the last pop but before the lock
        # was released; its worker would have exited on the held lock.
        if not _pending_path(repo).exists():
            return processed


def _repeats(explicit: int | None) -> int:
    if explicit:
        return explicit
    try:
        return int(os.environ.get(REPEATS_ENV, DEFAULT_REPEATS))
    except ValueError:
        return DEFAULT_REPEATS


def _locked_versions(code_dir: Path) -> dict[str, str]:
    import tomllib

    try:
        data = tomllib.loads((code_dir / "uv.lock").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return {pkg["name"]: pkg.get("version", "") for pkg in data.get("package", [])}


def _evals_overlay(code_dir: Path) -> list[str]:
    """``--with`` args so the suite can import pydantic-evals in ``code_dir``.

    pydantic-evals pins pydantic-ai-slim to its own version, so the overlay
    must match the commit's locked pydantic-ai-slim. Taking HEAD's version
    instead would silently upgrade an older commit's framework and hide
    exactly the change a ``framework`` comparison is meant to measure.
    """
    locked = _locked_versions(code_dir)
    if "pydantic-evals" in locked:
        return []
    slim = locked.get("pydantic-ai-slim")
    if not slim:
        raise SuiteRunError("Cannot find pydantic-ai-slim in the commit's uv.lock.")
    return ["--with", f"pydantic-evals=={slim}"]


@dataclass
class _Workspace:
    """Worktrees and a frozen copy of the suite for one job."""

    repo: Path
    root: Path
    worktrees: dict[str, Path] = field(default_factory=dict)

    def checkout(self, sha: str) -> Path:
        if sha not in self.worktrees:
            path = self.root / "worktrees" / sha[:12]
            gitutil.worktree_add(self.repo, path, sha)
            self.worktrees[sha] = path
        return self.worktrees[sha]

    def freeze_suite(self, sha: str) -> Path:
        """Copy the suite at ``sha`` so both sides of a comparison use it."""
        target = self.root / "suite"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(
            self.checkout(sha) / "evals",
            target / "evals",
            ignore=shutil.ignore_patterns("history", "__pycache__"),
        )
        return target

    def close(self) -> None:
        for path in self.worktrees.values():
            gitutil.worktree_remove(self.repo, path)
        shutil.rmtree(self.root, ignore_errors=True)


def run_suite_at(
    repo: Path,
    code_dir: Path,
    suite_root: Path,
    agent: str,
    *,
    repeats: int,
    run_id: str,
) -> dict[str, Any]:
    """Run ``agent``'s suite from ``suite_root`` against the code in ``code_dir``.

    ``uv run --project`` gives the commit its own locked environment; the
    suite is imported from ``suite_root`` (the working directory), so the
    commit's own ``evals/`` never shadows it.
    """
    uv = shutil.which("uv")
    if uv is None:
        raise SuiteRunError("uv is not on PATH.")
    runs_dir = state_dir(repo) / "runs" / run_id
    runs_dir.mkdir(parents=True, exist_ok=True)
    out = runs_dir / "result.json"
    argv = [uv, "run", "--quiet", "--frozen", "--project", str(code_dir)]
    argv += _evals_overlay(code_dir)
    argv += [
        "python",
        "-m",
        "evals",
        "run-suite",
        "--agent",
        agent,
        "--repeats",
        str(repeats),
        "--out",
        str(out),
        "--details",
        str(runs_dir / "cases"),
    ]
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED_ENV}
    env.pop("VIRTUAL_ENV", None)
    env["PYTHONPATH"] = str(suite_root)
    env[progress.PROGRESS_ENV] = str(progress_path(repo))
    budget = suite_timeout(suite_root, agent, repeats)
    # Own session so a timeout can kill the whole tree: uv, python, and any
    # command the agent started.
    process = subprocess.Popen(argv, cwd=suite_root, env=env, start_new_session=True)
    try:
        returncode = process.wait(timeout=budget)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        logger.warning("eval_suite_timed_out", agent=agent, timeout_seconds=budget)
        raise SuiteRunError(f"Suite run for {agent} timed out after {budget:.0f}s.")
    if returncode == EXIT_MODEL_UNAVAILABLE:
        raise ModelUnavailable(f"Model server unreachable for {agent}.")
    if returncode != 0 or not out.exists():
        raise SuiteRunError(f"Suite run for {agent} exited with {returncode}.")
    return json.loads(out.read_text())


def suite_timeout(suite_root: Path, agent: str, repeats: int) -> float:
    """Wall-clock budget for one suite run, from the cases' own timeouts."""
    override = os.environ.get(SUITE_TIMEOUT_ENV)
    if override:
        try:
            return float(override)
        except ValueError:
            logger.warning("eval_suite_timeout_invalid", env=SUITE_TIMEOUT_ENV)
    from evals.cases import load_suite

    cases = load_suite(agent, suite_root / "evals").cases
    per_repeat = sum(c.timeout_seconds + _CHECKS_ALLOWANCE_SECONDS for c in cases)
    return per_repeat * repeats + _SETUP_ALLOWANCE_SECONDS


def _autocommit_enabled() -> bool:
    return os.environ.get(AUTOCOMMIT_ENV, "1") != "0"


def process_job(
    repo: Path, job: Job, *, repeats: int | None = None, runner: Any = None
) -> list[Path]:
    """Run before/after for each agent in ``job`` and write history records.

    ``runner`` replaces :func:`run_suite_at` in tests.
    """
    runner = runner or run_suite_at
    repeats = _repeats(repeats)
    head = job.sha
    logger.info("eval_job_started", sha=head[:7], agents=job.agents, repeats=repeats)
    tracker = progress.JobProgress(
        progress_path(repo), sha=head, agents=job.agents, repeats=repeats
    )
    space = _Workspace(repo=repo, root=state_dir(repo) / "jobs" / uuid.uuid4().hex)
    written: list[Path] = []
    kinds_seen: set[str] = set()
    outcome = "failed"
    try:
        # A manual run on an old commit may predate the suite; use HEAD's.
        suite_sha = head
        if not (space.checkout(head) / "evals" / "cases").is_dir():
            suite_sha = gitutil.rev_parse(repo, "HEAD") or head
        suite_root = space.freeze_suite(suite_sha)
        suite_agents = set(list_agents(suite_root / "evals"))
        records = history.load_records(repo)

        # Plan first, so progress can say "step 2 of 4".
        plan: list[tuple[str, dict[str, Any] | None, str | None]] = []
        for agent in job.agents:
            if agent not in suite_agents:
                logger.info("eval_agent_has_no_cases", agent=agent)
                continue
            s_hash = suite_hash(agent, suite_root / "evals")
            baseline = history.find_baseline(
                repo, records, agent=agent, head=head, suite_hash=s_hash
            )
            if job.trigger == "model_check":
                latest = history.latest_record(records, agent)
                if latest and latest.get("suite_hash") == s_hash:
                    baseline = latest
            parent = None
            if baseline is None:
                parent = gitutil.parent_of(repo, head)
                if parent is None:
                    logger.info("eval_no_parent_for_baseline", agent=agent)
            plan.append((agent, baseline, parent))
        total = sum(2 if parent else 1 for _, _, parent in plan)
        step = 0
        progress.say(
            f"job {head[:7]}: {total} suite runs for {', '.join(a for a, _, _ in plan)}"
            + (
                " (no earlier results to compare with, so the parent "
                "commit is scored first)"
                if any(parent for _, _, parent in plan)
                else ""
            )
        )

        for agent, baseline, parent in plan:
            if parent is not None:
                step += 1
                tracker.step(step, total, agent=agent, side="before", sha=parent)
                when = datetime.now(timezone.utc)
                result = runner(
                    repo,
                    space.checkout(parent),
                    suite_root,
                    agent,
                    repeats=repeats,
                    run_id=history.make_run_id(when, parent, agent),
                )
                baseline = history.build_record(
                    repo=repo,
                    result=result,
                    sha=parent,
                    trigger="baseline",
                    change={"kinds": [], "files": [], "details": {}},
                    baseline=None,
                    when=when,
                )
                written.append(history.write_record(repo, baseline))

            step += 1
            tracker.step(step, total, agent=agent, side="after", sha=head)
            when = datetime.now(timezone.utc)
            result = runner(
                repo,
                space.checkout(head),
                suite_root,
                agent,
                repeats=repeats,
                run_id=history.make_run_id(when, head, agent),
            )
            change = _describe_change(repo, agent, baseline, head, result)
            kinds_seen.update(change["kinds"])
            record = history.build_record(
                repo=repo,
                result=result,
                sha=head,
                trigger=job.trigger,
                change=change,
                baseline=baseline,
                when=when,
            )
            written.append(history.write_record(repo, record))
            summary = history.one_line_summary(record)
            logger.info("eval_record_written", run_id=record["run_id"])
            _append_summary(repo, summary)
            progress.say(summary)
        outcome = "finished"
    except ModelUnavailable:
        logger.warning("eval_job_skipped", sha=head[:7], reason="model_unavailable")
        _append_summary(repo, f"SKIPPED {head[:7]}: model server not reachable")
        outcome = "skipped: model server not reachable"
    finally:
        space.close()
        tracker.finish(outcome)

    if written:
        _commit_records(repo, job, written, sorted(kinds_seen))
    return written


def _describe_change(
    repo: Path,
    agent: str,
    baseline: dict[str, Any] | None,
    head: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    if baseline is None:
        return {"kinds": [], "files": [], "details": {}, "base": None}
    base_sha = baseline["commit"]["sha"]
    changeset = (
        changes.detect_changes(repo, base_sha, head, [agent])
        if base_sha != head
        else changes.ChangeSet(base=base_sha, head=head)
    )
    changes.add_model_version_change(
        changeset,
        agent,
        result["env"].get("model"),
        (baseline.get("env") or {}).get("model_digest"),
        result["env"].get("model_digest"),
    )
    described = changeset.for_agent(agent)
    described["base"] = base_sha
    return described


def _commit_records(repo: Path, job: Job, paths: list[Path], kinds: list[str]) -> None:
    relative = [str(p.relative_to(repo)) for p in paths]
    reason = None
    if not _autocommit_enabled():
        reason = "autocommit_disabled"
    elif gitutil.operation_in_progress(repo):
        reason = "git_operation_in_progress"
    elif gitutil.current_branch(repo) is None:
        reason = "detached_head"
    elif job.branch and gitutil.current_branch(repo) != job.branch:
        reason = "branch_changed"
    if reason is not None:
        logger.info("eval_records_left_uncommitted", reason=reason, files=len(paths))
        _append_summary(
            repo, f"results left uncommitted ({reason}); run: python -m evals record"
        )
        return
    agents = ", ".join(sorted({p.stem.rsplit("_", 1)[-1] for p in paths}))
    suffix = f" ({', '.join(kinds)})" if kinds else ""
    message = f"evals: {agents} results for {job.sha[:7]}{suffix}"
    if gitutil.commit_paths(repo, relative, message):
        logger.info("eval_records_committed", files=len(paths))


def record_uncommitted(repo: Path) -> bool:
    """Commit history files left behind (e.g. written during a rebase)."""
    out = gitutil.git(
        repo,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        str(history.history_dir(repo)),
    )
    paths = [line[3:] for line in out.splitlines() if line.endswith(".json")]
    if not paths:
        return False
    return gitutil.commit_paths(repo, paths, "evals: record pending results")


# --- model check ----------------------------------------------------------


def check_models(repo: Path) -> Job | None:
    """Queue a run for agents whose Ollama model digest moved since their
    last record (``ollama pull`` rebuilt a tag with no commit)."""
    from evals.runner import ModelUnavailableError, model_env, resolve_spec_model
    from vikram.settings import VikramSettings

    records = history.load_records(repo)
    moved: list[str] = []
    for agent in list_agents(repo / "evals"):
        latest = history.latest_record(records, agent)
        if latest is None:
            continue
        spec, settings = resolve_spec_model(agent, VikramSettings())
        try:
            env = model_env(spec, settings)
        except ModelUnavailableError:
            logger.warning("eval_model_check_skipped", agent=agent)
            continue
        before = (latest.get("env") or {}).get("model_digest")
        if env["model_digest"] and before and env["model_digest"] != before:
            moved.append(agent)
    if not moved:
        return None
    head = gitutil.rev_parse(repo, "HEAD")
    if head is None:
        return None
    job = enqueue(
        repo,
        Job(
            sha=head,
            branch=gitutil.current_branch(repo),
            agents=moved,
            trigger="model_check",
        ),
    )
    spawn_worker(repo)
    return job
