"""Hard checks: yes/no assertions about one agent run.

Each check sees an :class:`Observation` (final output, tool calls and results,
and the workspace before and after) and returns pass or fail. Check names are
stable because they are stored in committed history records.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from evals.cases import CheckSpec


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: str  # JSON-encoded arguments, for substring matching


@dataclass(frozen=True)
class ToolResult:
    name: str
    content: str


@dataclass
class Observation:
    output: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    workspace: Path | None = None
    # Relative path -> bytes, captured after setup and before the run.
    initial_files: dict[str, bytes] = field(default_factory=dict)
    today: date = field(default_factory=lambda: datetime.now().astimezone().date())


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool


class CheckError(ValueError):
    """Raised when a check spec is malformed."""


CheckFn = Callable[[CheckSpec, Observation], bool]
_CHECKS: dict[str, CheckFn] = {}


def check(name: str) -> Callable[[CheckFn], CheckFn]:
    def register(fn: CheckFn) -> CheckFn:
        _CHECKS[name] = fn
        return fn

    return register


def check_names() -> list[str]:
    return sorted(_CHECKS)


def check_label(spec: CheckSpec) -> str:
    """Short stable label, e.g. ``tool_called:run_command`` or
    ``file_contains:inventory/dates.py``. Stored in history records."""
    target = (
        spec.tool
        or ("|".join(spec.any_of) if spec.any_of else None)
        or spec.path
        or spec.value
        or (spec.values[0] if spec.values else None)
        or spec.pattern
        or (",".join(spec.paths) if spec.paths else None)
    )
    label = f"{spec.type}:{target}" if target else spec.type
    return label if len(label) <= 60 else label[:57] + "..."


def run_check(spec: CheckSpec, obs: Observation) -> CheckResult:
    fn = _CHECKS.get(spec.type)
    if fn is None:
        raise CheckError(f"Unknown check type {spec.type!r}.")
    return CheckResult(name=check_label(spec), passed=bool(fn(spec, obs)))


def _needles(spec: CheckSpec) -> list[str]:
    needles = [*spec.values]
    if spec.value is not None:
        needles.append(spec.value)
    if not needles:
        raise CheckError(f"{spec.type} needs value or values.")
    return needles


def _workspace(obs: Observation) -> Path:
    if obs.workspace is None:
        raise CheckError("This check needs a workspace.")
    return obs.workspace


def _snapshot(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] in {".git", "__pycache__"}:
            continue
        if "__pycache__" in relative.parts or ".pytest_cache" in relative.parts:
            continue
        files[relative.as_posix()] = path.read_bytes()
    return files


def snapshot_workspace(root: Path) -> dict[str, bytes]:
    return _snapshot(root)


def _tool_matches(call_name: str, spec: CheckSpec) -> bool:
    names = [*spec.any_of]
    if spec.tool:
        names.append(spec.tool)
    return not names or call_name in names


# --- output ---------------------------------------------------------------


@check("output_contains")
def _output_contains(spec: CheckSpec, obs: Observation) -> bool:
    """Every needle appears in the output (case-insensitive)."""
    output = obs.output.lower()
    return all(needle.lower() in output for needle in _needles(spec))


@check("output_contains_any")
def _output_contains_any(spec: CheckSpec, obs: Observation) -> bool:
    output = obs.output.lower()
    return any(needle.lower() in output for needle in _needles(spec))


@check("output_not_contains")
def _output_not_contains(spec: CheckSpec, obs: Observation) -> bool:
    output = obs.output.lower()
    return not any(needle.lower() in output for needle in _needles(spec))


@check("output_matches")
def _output_matches(spec: CheckSpec, obs: Observation) -> bool:
    if not spec.pattern:
        raise CheckError("output_matches needs pattern.")
    return re.search(spec.pattern, obs.output, re.IGNORECASE | re.MULTILINE) is not None


@check("mentions_today")
def _mentions_today(spec: CheckSpec, obs: Observation) -> bool:
    """The output names today's date in any common format."""
    today = obs.today
    month = today.strftime("%B")
    forms = {
        today.isoformat(),
        f"{month} {today.day}",
        f"{today.day} {month}",
        f"{today.strftime('%b')} {today.day}",
        f"{today.day} {today.strftime('%b')}",
        f"{today.month}/{today.day}/{today.year}",
        f"{today.day}/{today.month}/{today.year}",
    }
    output = obs.output.lower()
    return any(form.lower() in output for form in forms)


# --- tools ----------------------------------------------------------------


@check("tool_called")
def _tool_called(spec: CheckSpec, obs: Observation) -> bool:
    """A matching tool was called (optionally with a substring in its args)."""
    for call in obs.tool_calls:
        if not _tool_matches(call.name, spec):
            continue
        if spec.args_contains and spec.args_contains.lower() not in call.args.lower():
            continue
        return True
    return False


@check("tool_not_called")
def _tool_not_called(spec: CheckSpec, obs: Observation) -> bool:
    return not _tool_called(spec, obs)


@check("tool_result_contains")
def _tool_result_contains(spec: CheckSpec, obs: Observation) -> bool:
    needles = [n.lower() for n in _needles(spec)]
    return any(
        _tool_matches(result.name, spec)
        and all(n in result.content.lower() for n in needles)
        for result in obs.tool_results
    )


@check("no_tool_result_contains")
def _no_tool_result_contains(spec: CheckSpec, obs: Observation) -> bool:
    needles = [n.lower() for n in _needles(spec)]
    return not any(
        _tool_matches(result.name, spec)
        and any(n in result.content.lower() for n in needles)
        for result in obs.tool_results
    )


# --- workspace ------------------------------------------------------------


@check("file_contains")
def _file_contains(spec: CheckSpec, obs: Observation) -> bool:
    if not spec.path:
        raise CheckError("file_contains needs path.")
    path = _workspace(obs) / spec.path
    if not path.is_file():
        return False
    text = path.read_text(errors="replace")
    return all(needle in text for needle in _needles(spec))


@check("workspace_not_contains")
def _workspace_not_contains(spec: CheckSpec, obs: Observation) -> bool:
    """No file matching ``paths`` globs (default all) contains any needle."""
    needles = _needles(spec)
    globs = spec.paths or ["*"]
    for relative, content in _snapshot(_workspace(obs)).items():
        if not any(fnmatch.fnmatch(relative, pattern) for pattern in globs):
            continue
        text = content.decode(errors="replace")
        if any(needle in text for needle in needles):
            return False
    return True


@check("files_unchanged")
def _files_unchanged(spec: CheckSpec, obs: Observation) -> bool:
    """Files matching ``paths`` globs (default all) are byte-identical and
    none were deleted."""
    after = _snapshot(_workspace(obs))
    globs = spec.paths or ["*"]
    for relative, content in obs.initial_files.items():
        if not any(fnmatch.fnmatch(relative, pattern) for pattern in globs):
            continue
        if after.get(relative) != content:
            return False
    return True


def _pytest(workspace: Path, targets: list[str]) -> bool:
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *targets],
            cwd=workspace,
            capture_output=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    return completed.returncode == 0


@check("tests_pass")
def _tests_pass(spec: CheckSpec, obs: Observation) -> bool:
    """The workspace's own tests pass (optionally only ``paths``)."""
    return _pytest(_workspace(obs), spec.paths)


@check("hidden_test")
def _hidden_test(spec: CheckSpec, obs: Observation) -> bool:
    """A test the agent never saw passes against its changes."""
    if not spec.code:
        raise CheckError("hidden_test needs code.")
    workspace = _workspace(obs)
    hidden = workspace / "_eval_hidden_test.py"
    hidden.write_text(spec.code)
    try:
        return _pytest(workspace, [hidden.name])
    finally:
        hidden.unlink(missing_ok=True)
