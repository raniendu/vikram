"""Run one agent's eval suite and aggregate metrics.

Runs in whatever checkout it is imported from: the orchestrator starts it in a
git worktree of the commit under test, so ``vikram`` and ``spec/`` come from
that commit while the cases come from this suite.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import statistics
import subprocess
import tempfile
import time
from contextlib import AsyncExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import httpx

from evals import FIXTURES_DIR
from evals.cases import CaseSpec, SetupSpec, load_suite, suite_hash
from evals.checks import (
    CheckResult,
    Observation,
    ToolCall,
    ToolResult,
    run_check,
    snapshot_workspace,
)
from evals.judge import JudgeModel, build_judge_model, judge
from vikram.logging import get_logger

logger = get_logger(__name__)

NEEDS_WEB_TAG = "needs_web"

# Approval policy for unattended runs. Writes stay inside a throwaway copy of
# the fixture, so file edits and delegation are approved. Of the commands that
# need approval (Tier 2), only test runners are approved; anything else is
# denied, as a cautious human would. Approving e.g. ``cat`` would let a
# "read ../../etc/passwd" case pass through the approval gate unrealistically.
APPROVED_TOOLS = frozenset({"write_file", "edit_file", "delegate_to_agent"})
APPROVED_EXECUTABLES = frozenset({"python", "python3", "pytest"})


class ModelUnavailableError(RuntimeError):
    """The model server for this agent cannot be reached."""


@dataclass
class RepeatResult:
    passed: bool
    checks: list[CheckResult] = field(default_factory=list)
    judge_score: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    requests: int | None = None
    latency_ms: float = 0.0
    tool_calls: int = 0
    error_type: str | None = None
    # Local-only detail; never written to committed history.
    output: str = ""
    trace: list[dict[str, str]] = field(default_factory=list)


# --- model environment ----------------------------------------------------


def ollama_root(base_url: str) -> str:
    root = base_url.rstrip("/")
    return root[: -len("/v1")] if root.endswith("/v1") else root


def ollama_digest(base_url: str, model: str, *, timeout: float = 2.0) -> str | None:
    """Digest of a locally pulled Ollama model, or None if unknown.

    Raises ModelUnavailableError when the server does not answer.
    """
    try:
        response = httpx.get(f"{ollama_root(base_url)}/api/tags", timeout=timeout)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ModelUnavailableError("Ollama is not reachable.") from exc
    wanted = model if ":" in model else f"{model}:latest"
    for entry in response.json().get("models", []):
        if entry.get("name") in {model, wanted} or entry.get("model") in {
            model,
            wanted,
        }:
            return entry.get("digest")
    return None


def resolve_spec_model(agent: str, settings: Any) -> tuple[Any, Any]:
    """Load the spec and resolve its model the way a fresh install would.

    Evals measure the specs as committed, so env overrides (VIKRAM_MODEL...)
    and the user's saved ``/model`` choices are cleared. Clearing rather than
    pinning keeps delegated subagents on their own spec models.
    """
    from vikram.specstore import load_agent

    settings = settings.model_copy(
        update={"model_provider": None, "model": None, "agent_overrides": {}}
    )
    spec = load_agent(agent, settings)
    return spec, settings


def model_env(agent_spec: Any, settings: Any) -> dict[str, Any]:
    from vikram.settings import resolve_agent_model_selection

    provider, model = resolve_agent_model_selection(
        settings,
        agent_id=agent_spec.agent_dir.name,
        spec_provider=agent_spec.model_provider,
        spec_model=agent_spec.model,
    )
    digest = None
    if provider == "ollama" and model:
        digest = ollama_digest(settings.ollama_base_url, model)
    return {"provider": provider, "model": model, "model_digest": digest}


def package_versions() -> dict[str, str | None]:
    from importlib.metadata import PackageNotFoundError, version

    versions: dict[str, str | None] = {}
    for name in ("pydantic-ai-slim", "pydantic-ai-harness", "pydantic-evals"):
        try:
            versions[name] = version(name)
        except PackageNotFoundError:
            versions[name] = None
    return versions


# --- workspace ------------------------------------------------------------


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=vikram-evals",
            "-c",
            "user.email=evals@localhost",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=workspace,
        check=True,
        capture_output=True,
    )


def prepare_workspace(root: Path, workspace: str | None, setup: SetupSpec) -> None:
    if workspace:
        shutil.copytree(FIXTURES_DIR / workspace, root, dirs_exist_ok=True)
    if setup.git_init:
        _git(root, "init", "-q")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "Initial fixture")
    for relative, content in setup.write.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    for gen in setup.generate:
        target = root / gen.path
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            gen.inject.get(number, gen.line.format(n=number))
            for number in range(1, gen.lines + 1)
        ]
        target.write_text("\n".join(lines) + "\n")


@contextmanager
def _chdir(path: Path) -> Iterator[None]:
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


# --- one run --------------------------------------------------------------


def _approve(request: Any) -> str:
    if request.tool_name in APPROVED_TOOLS:
        return "yes"
    if request.tool_name == "run_command":
        import shlex

        try:
            argv = shlex.split(str(request.args.get("command", "")))
        except ValueError:
            return "no"
        if argv and Path(argv[0]).name in APPROVED_EXECUTABLES:
            return "yes"
    return "no"


def _read_usage(result: Any, repeat: RepeatResult) -> None:
    # Same tolerant read as vikram.playground._read_usage: usage is a method
    # on some result types and an attribute on others.
    usage = getattr(result, "usage", None)
    if callable(usage):
        usage = usage()
    repeat.input_tokens = getattr(usage, "input_tokens", None)
    repeat.output_tokens = getattr(usage, "output_tokens", None)
    repeat.requests = getattr(usage, "requests", None)


def _collect_trace(result: Any, obs: Observation, repeat: RepeatResult) -> None:
    from pydantic_ai.messages import ToolCallPart, ToolReturnPart

    for message in result.all_messages():
        for part in getattr(message, "parts", []):
            if isinstance(part, ToolCallPart):
                args = part.args_as_json_str()
                obs.tool_calls.append(ToolCall(name=part.tool_name, args=args))
                repeat.trace.append(
                    {"kind": "call", "tool": part.tool_name, "args": args}
                )
            elif isinstance(part, ToolReturnPart):
                content = part.model_response_str()
                obs.tool_results.append(
                    ToolResult(name=part.tool_name, content=content)
                )
                repeat.trace.append(
                    {"kind": "result", "tool": part.tool_name, "content": content}
                )
    repeat.tool_calls = len(obs.tool_calls)


async def run_case_once(
    case: CaseSpec,
    spec: Any,
    settings: Any,
    judge_model: JudgeModel | None,
    *,
    build_agent: Any = None,
) -> RepeatResult:
    if build_agent is None:
        from vikram.agent import build_agent

    repeat = RepeatResult(passed=False)
    with tempfile.TemporaryDirectory(prefix="vikram-eval-") as tmp:
        root = Path(tmp).resolve()
        prepare_workspace(root, case.workspace, case.setup)
        obs = Observation(
            output="", workspace=root, initial_files=snapshot_workspace(root)
        )
        with _chdir(root):
            agent = build_agent(
                spec,
                settings,
                surface=case.surface,
                approval_request=_approve,
            )
            started = time.perf_counter()
            try:
                async with AsyncExitStack() as stack:
                    if agent.mcp_clients:
                        await stack.enter_async_context(agent)
                    result = await asyncio.wait_for(
                        agent.run(case.prompt), timeout=case.timeout_seconds
                    )
            except Exception as exc:  # recorded as a failed repeat, not raised
                repeat.latency_ms = round((time.perf_counter() - started) * 1000, 1)
                repeat.error_type = type(exc).__name__
                logger.exception(
                    "eval_case_run_failed", case_id=case.id, error_type=type(exc).__name__
                )
                return repeat
            repeat.latency_ms = round((time.perf_counter() - started) * 1000, 1)
            obs.output = str(result.output)
            repeat.output = obs.output
            _read_usage(result, repeat)
            _collect_trace(result, obs, repeat)

            repeat.checks = [run_check(check, obs) for check in case.checks]

        passed = all(check.passed for check in repeat.checks)
        if case.judge is not None:
            if judge_model is None:
                raise RuntimeError(f"Case {case.id} needs a judge model.")
            try:
                verdict = await judge(
                    prompt=case.prompt,
                    output=obs.output,
                    rubric=case.judge.rubric,
                    threshold=case.judge.threshold,
                    model=judge_model,
                )
            except Exception as exc:
                logger.exception("eval_judge_failed", case_id=case.id)
                repeat.error_type = f"judge:{type(exc).__name__}"
                return repeat
            repeat.judge_score = verdict.score
            passed = passed and verdict.passed
        repeat.passed = passed
    return repeat


# --- aggregation ----------------------------------------------------------


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 4) if values else None


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def aggregate_case(case_id: str, repeats: list[RepeatResult]) -> dict[str, Any]:
    failed: dict[str, int] = {}
    for repeat in repeats:
        for check in repeat.checks:
            if not check.passed:
                failed[check.name] = failed.get(check.name, 0) + 1
    tokens = [
        (r.input_tokens or 0) + (r.output_tokens or 0)
        for r in repeats
        if r.error_type is None and r.input_tokens is not None
    ]
    judged = [r.judge_score for r in repeats if r.judge_score is not None]
    return {
        "id": case_id,
        "status": "ok",
        "repeats": len(repeats),
        "pass_rate": round(sum(r.passed for r in repeats) / len(repeats), 4),
        "judge_mean": _mean(judged),
        "tokens_mean": _mean([float(t) for t in tokens]),
        "latency_p50_ms": _median([r.latency_ms for r in repeats]),
        "tool_calls_mean": _mean([float(r.tool_calls) for r in repeats]),
        "requests_mean": _mean([float(r.requests) for r in repeats if r.requests]),
        "failed_checks": dict(sorted(failed.items())),
        "errors": sum(r.error_type is not None for r in repeats),
        "error_types": sorted({r.error_type for r in repeats if r.error_type}),
    }


def summarize(cases: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [c for c in cases if c.get("status") == "ok"]

    def values(key: str) -> list[float]:
        return [c[key] for c in scored if c.get(key) is not None]

    return {
        "cases": len(scored),
        "skipped": sum(c.get("status") == "skipped" for c in cases),
        "pass_rate": _mean(values("pass_rate")),
        "judge_mean": _mean(values("judge_mean")),
        "tokens_mean": _mean(values("tokens_mean")),
        "latency_p50_ms": _median(values("latency_p50_ms")),
        "tool_calls_mean": _mean(values("tool_calls_mean")),
        "errors": sum(c.get("errors", 0) for c in scored),
    }


# --- whole suite ----------------------------------------------------------


def _web_available(settings: Any) -> bool:
    return bool(getattr(settings, "parallel_api_key", None))


async def run_suite(
    agent: str,
    *,
    repeats: int = 3,
    case_filter: list[str] | None = None,
    details_dir: Path | None = None,
    settings: Any = None,
    judge_model: JudgeModel | None = None,
    build_agent: Any = None,
) -> dict[str, Any]:
    from vikram.settings import VikramSettings

    suite = load_suite(agent)
    settings = settings or VikramSettings()
    spec, settings = resolve_spec_model(agent, settings)
    env = model_env(spec, settings)
    cases = [c for c in suite.cases if not case_filter or c.id in case_filter]
    if judge_model is None and any(c.judge for c in cases):
        judge_model = build_judge_model(settings)

    results: list[dict[str, Any]] = []
    for case in cases:
        if NEEDS_WEB_TAG in case.tags and not _web_available(settings):
            results.append({"id": case.id, "status": "skipped", "reason": "needs_web"})
            logger.info("eval_case_skipped", case_id=case.id, reason="needs_web")
            continue
        runs: list[RepeatResult] = []
        for index in range(repeats):
            logger.info("eval_case_started", case_id=case.id, repeat=index + 1)
            runs.append(
                await run_case_once(
                    case, spec, settings, judge_model, build_agent=build_agent
                )
            )
        aggregated = aggregate_case(case.id, runs)
        results.append(aggregated)
        logger.info(
            "eval_case_finished",
            case_id=case.id,
            pass_rate=aggregated["pass_rate"],
            errors=aggregated["errors"],
        )
        if details_dir is not None:
            _write_details(details_dir, case, runs)

    return {
        "agent": agent,
        "suite_hash": suite_hash(agent),
        "repeats": repeats,
        "env": {
            **env,
            "judge_model": judge_model.label if judge_model else None,
            "versions": package_versions(),
        },
        "summary": summarize(results),
        "cases": results,
    }


def _write_details(details_dir: Path, case: CaseSpec, runs: list[RepeatResult]) -> None:
    details_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": case.id,
        "prompt": case.prompt,
        "repeats": [
            {
                "passed": r.passed,
                "checks": {c.name: c.passed for c in r.checks},
                "judge_score": r.judge_score,
                "latency_ms": r.latency_ms,
                "error_type": r.error_type,
                "output": r.output,
                "trace": r.trace,
            }
            for r in runs
        ],
    }
    (details_dir / f"{case.id}.json").write_text(
        json.dumps(payload, indent=2, default=str)
    )
