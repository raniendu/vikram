"""Lifecycle hooks for Vikram agents.

Agents declare hooks in ``agent.toml`` under ``[[hooks]]``. Each entry runs at
one of four lifecycle events and can observe, augment, or block what the agent
is about to do:

* ``PreToolUse``       — before a tool runs; may block the call.
* ``PostToolUse``      — after a tool returns; may append context or block.
* ``UserPromptSubmit`` — when a prompt enters a run; may inject context or block.
* ``Stop``             — when a run finishes; advisory (for notifications/logging).

A hook handler is one of two transports:

* ``command`` — an external program. It receives the event payload as JSON on
  stdin and may return a JSON decision on stdout. Following the convention of
  other CLI agents, exit code ``2`` blocks the action (stderr is the reason),
  exit code ``0`` allows it (stdout JSON may still refine the decision), and any
  other non-zero exit is a non-blocking error that is logged and ignored.
* ``python`` — an in-process callable referenced as ``module:function``. It is
  imported when the agent is built (so misconfiguration fails loudly) and called
  with the payload ``dict``; it may be sync or async and returns ``None``, a
  decision ``dict``, or a ``str`` (treated as additional context).

Decision payloads use these keys, all optional::

    {"decision": "allow" | "deny" | "block",  # deny/block stop the action
     "reason": "...",                          # shown to the model when blocked
     "additional_context": "..."}              # injected/appended context

Like MCP server specs, string fields (``command``, ``args``, ``cwd``, and the
values of ``env``) may reference environment variables with ``${VAR}`` syntax,
expanded when the agent is built; a missing variable is a hard error. This keeps
specs safe to commit while real secrets stay in ``.env`` or the environment.

Tool and run events are wired by ``vikram.agent`` into Strands hooks. The hook
transport and decision model stay Vikram-owned so existing ``[[hooks]]`` TOML
continues to work across runtime migrations.
"""

from __future__ import annotations

import asyncio
import fnmatch
import importlib
import inspect
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from vikram.logging import get_logger

logger = get_logger(__name__)

HookEvent = Literal["PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"]
HookTransport = Literal["command", "python"]

TOOL_EVENTS: tuple[HookEvent, ...] = ("PreToolUse", "PostToolUse")
DEFAULT_HOOK_TIMEOUT = 30.0
# Exit code an external hook uses to block the action (stderr is the reason).
BLOCK_EXIT_CODE = 2

# Matches ${NAME} references for environment-variable expansion.
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class HookConfigError(RuntimeError):
    """Raised when a hook spec is invalid or references a missing env var."""


class HookBlockedError(RuntimeError):
    """Raised when a ``UserPromptSubmit`` hook blocks a run before it starts."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class HookSpec(BaseModel):
    """Declarative configuration for one lifecycle hook.

    Lives in ``agent.toml`` under ``[[hooks]]``. Transport-specific fields are
    validated when the agent is built (by :func:`build_hooks`) rather than at
    parse time, so loading a spec never requires the referenced hooks' secrets.
    """

    event: HookEvent
    transport: HookTransport = "command"
    # Glob matched against the tool name; applies to tool events only.
    matcher: str = "*"

    # command transport: an external program speaking JSON over stdin/stdout.
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None

    # python transport: an in-process callable referenced as "module:function".
    entrypoint: str | None = None

    # Shared knob: seconds to wait before abandoning the hook (non-blocking).
    timeout: float = DEFAULT_HOOK_TIMEOUT


@dataclass(frozen=True)
class _CommandHook:
    event: HookEvent
    matcher: str
    argv: list[str]
    env: dict[str, str] | None
    cwd: str | None
    timeout: float


@dataclass(frozen=True)
class _PythonHook:
    event: HookEvent
    matcher: str
    func: Callable[[dict[str, Any]], Any]
    timeout: float


Hook = _CommandHook | _PythonHook


@dataclass(frozen=True)
class HookSet:
    """Hooks grouped by lifecycle event, ready to run."""

    pre: tuple[Hook, ...] = ()
    post: tuple[Hook, ...] = ()
    user_prompt_submit: tuple[Hook, ...] = ()
    stop: tuple[Hook, ...] = ()

    @property
    def has_tool_hooks(self) -> bool:
        return bool(self.pre or self.post)

    @property
    def has_run_hooks(self) -> bool:
        return bool(self.user_prompt_submit or self.stop)


@dataclass
class HookDecision:
    """Aggregated outcome of running every hook for one event."""

    blocked: bool = False
    reasons: list[str] = field(default_factory=list)
    added_context: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "; ".join(r for r in self.reasons if r)

    @property
    def context(self) -> str:
        return "\n\n".join(c for c in self.added_context if c)


def _expand(value: str, environ: Mapping[str, str], *, where: str) -> str:
    """Expand ``${VAR}`` references in ``value`` from ``environ``.

    Raises :class:`HookConfigError` naming ``where`` if a referenced variable is
    not defined, so misconfiguration fails loudly at agent-build time.
    """

    def replace(match: re.Match[str]) -> str:
        var = match.group(1)
        if var not in environ:
            raise HookConfigError(
                f"{where} references undefined environment variable "
                f"${{{var}}}. Set it in the environment or .env."
            )
        return environ[var]

    return _ENV_REF.sub(replace, value)


def _import_entrypoint(ref: str, *, where: str) -> Callable[[dict[str, Any]], Any]:
    """Import a ``module:function`` reference into a callable."""
    if ":" not in ref:
        raise HookConfigError(
            f"{where} entrypoint {ref!r} must be in 'module:function' form."
        )
    module_name, _, attr = ref.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise HookConfigError(
            f"{where} could not import module {module_name!r}: {exc}"
        ) from exc
    func = getattr(module, attr, None)
    if func is None:
        raise HookConfigError(
            f"{where} module {module_name!r} has no attribute {attr!r}."
        )
    if not callable(func):
        raise HookConfigError(f"{where} entrypoint {ref!r} is not callable.")
    return func


def _compile_hook(spec: HookSpec, environ: Mapping[str, str]) -> Hook:
    where = f"Hook {spec.event!r}"
    if spec.transport == "command":
        if not spec.command:
            raise HookConfigError(
                f"{where} uses command transport but has no 'command'."
            )
        argv = [_expand(spec.command, environ, where=f"{where} command")]
        argv += [_expand(arg, environ, where=f"{where} args") for arg in spec.args]
        env = {
            key: _expand(value, environ, where=f"{where} env[{key!r}]")
            for key, value in spec.env.items()
        }
        cwd = _expand(spec.cwd, environ, where=f"{where} cwd") if spec.cwd else None
        return _CommandHook(
            event=spec.event,
            matcher=spec.matcher,
            argv=argv,
            env=env or None,
            cwd=cwd,
            timeout=spec.timeout,
        )
    if spec.transport == "python":
        if not spec.entrypoint:
            raise HookConfigError(
                f"{where} uses python transport but has no 'entrypoint'."
            )
        func = _import_entrypoint(spec.entrypoint, where=where)
        return _PythonHook(
            event=spec.event,
            matcher=spec.matcher,
            func=func,
            timeout=spec.timeout,
        )
    raise HookConfigError(f"{where} has unknown transport {spec.transport!r}.")


def build_hooks(
    specs: list[HookSpec], environ: Mapping[str, str] | None = None
) -> HookSet:
    """Compile every configured hook into a :class:`HookSet`.

    ``environ`` defaults to ``os.environ`` and is used to expand ``${VAR}``
    references. Python hooks are imported here so a bad ``entrypoint`` fails at
    agent-build time rather than mid-run.
    """
    environ = os.environ if environ is None else environ
    buckets: dict[HookEvent, list[Hook]] = {
        "PreToolUse": [],
        "PostToolUse": [],
        "UserPromptSubmit": [],
        "Stop": [],
    }
    for spec in specs:
        buckets[spec.event].append(_compile_hook(spec, environ))
    return HookSet(
        pre=tuple(buckets["PreToolUse"]),
        post=tuple(buckets["PostToolUse"]),
        user_prompt_submit=tuple(buckets["UserPromptSubmit"]),
        stop=tuple(buckets["Stop"]),
    )


def _apply_response(decision: HookDecision, raw: Any) -> None:
    """Fold one handler response into the aggregated decision."""
    if not isinstance(raw, Mapping):
        return
    verdict = str(raw.get("decision", "")).strip().lower()
    if verdict in ("deny", "block"):
        decision.blocked = True
        decision.reasons.append(str(raw.get("reason") or "A hook blocked this action."))
    context = raw.get("additional_context")
    if context:
        decision.added_context.append(str(context))


async def _run_command_hook(hook: _CommandHook, payload: dict[str, Any]) -> Any:
    data = json.dumps(payload).encode("utf-8")
    env = {**os.environ, **hook.env} if hook.env else None
    try:
        process = await asyncio.create_subprocess_exec(
            *hook.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=hook.cwd,
            env=env,
        )
    except (FileNotFoundError, OSError) as exc:
        logger.warning("hook_command_unstartable", argv=hook.argv, error=str(exc))
        return None

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(data), timeout=hook.timeout
        )
    except TimeoutError:
        process.kill()
        await process.communicate()
        logger.warning("hook_command_timeout", argv=hook.argv, timeout=hook.timeout)
        return None

    stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
    stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
    code = process.returncode

    if code == BLOCK_EXIT_CODE:
        return {"decision": "deny", "reason": stderr or "Hook blocked this action."}
    if code != 0:
        logger.warning(
            "hook_command_failed", argv=hook.argv, exit_code=code, stderr=stderr[:500]
        )
        return None
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        # A hook that prints plain text contributes it as additional context.
        return {"additional_context": stdout}


async def _run_python_hook(hook: _PythonHook, payload: dict[str, Any]) -> Any:
    try:
        result = hook.func(payload)
        if inspect.isawaitable(result):
            result = await asyncio.wait_for(result, timeout=hook.timeout)
    except Exception:
        logger.exception(
            "hook_python_error", entrypoint=getattr(hook.func, "__name__", "?")
        )
        return None
    if result is None or isinstance(result, Mapping):
        return result
    if isinstance(result, str):
        return {"additional_context": result}
    logger.warning("hook_python_bad_return", returned=type(result).__name__)
    return None


async def _run_one(hook: Hook, payload: dict[str, Any]) -> Any:
    if isinstance(hook, _CommandHook):
        return await _run_command_hook(hook, payload)
    return await _run_python_hook(hook, payload)


async def run_hooks(
    hooks: Sequence[Hook], payload: dict[str, Any], *, tool_name: str | None = None
) -> HookDecision:
    """Run every hook whose matcher applies and aggregate their decisions.

    Hooks run sequentially in declaration order; a single ``deny``/``block``
    response marks the whole decision blocked, and all reasons and additional
    context are collected.
    """
    decision = HookDecision()
    for hook in hooks:
        if tool_name is not None and not fnmatch.fnmatch(tool_name, hook.matcher):
            continue
        _apply_response(decision, await _run_one(hook, payload))
    return decision


def _stringify(result: Any) -> str:
    return result if isinstance(result, str) else str(result)
