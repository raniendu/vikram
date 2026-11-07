"""Compose approved tools with Python running only in the existing isolated VM."""

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import jsonschema
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vikram.sandbox import (
    COMPOSITION_FRAME_LIMIT,
    CompositionSandbox,
    SandboxUnavailable,
)

MAX_CODE = 20_000
MAX_OUTPUT = 8_000
MAX_CALLS = 100
ACTIVE_TIMEOUT = 5 * 60
TOTAL_TIMEOUT = 30 * 60
POLL_INTERVAL = 0.01
_CLOCK = ContextVar("vikram_composition_clock", default=None)

# This string is written to the VM. No model code is evaluated by the host.
GUEST_RUNNER = r"""
import ast, asyncio, inspect, json, os, pathlib, re, sys, traceback, uuid
root = pathlib.Path(sys.argv[1]).resolve()
tools = json.loads((root / "tools.json").read_text())
pending = {}
sent = 0

async def call_tool(name, *, _call_id=None, **arguments):
    global sent
    identifier = _call_id or uuid.uuid4().hex
    if not isinstance(identifier, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", identifier):
        raise ValueError("A call identifier must be 1-100 ASCII letters, digits, _ or -")
    frame = {"id": identifier, "name": name, "arguments": arguments}
    encoded = json.dumps(frame, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode()) > 1024 * 1024:
        raise ValueError("Tool request exceeds the broker frame limit")
    if identifier in pending:
        previous, future = pending[identifier]
        if previous != encoded:
            raise ValueError("A call identifier cannot be reused with different arguments")
        return await asyncio.shield(future)
    if sent >= 100:
        raise ValueError("Composition call limit reached")
    sent += 1
    future = asyncio.get_running_loop().create_future()
    pending[identifier] = encoded, future
    temporary = root / (identifier + ".tmp")
    temporary.write_text(encoded)
    temporary.replace(root / (identifier + ".request"))
    try:
        response = root / (identifier + ".response")
        while not response.exists():
            await asyncio.sleep(.02)
        with response.open("rb") as source:
            data = source.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("Tool response exceeds the broker frame limit")
        result = json.loads(data)
        future.set_result(result)
        return result
    except BaseException:
        future.cancel()
        raise

remaining = [8000]
class BoundedOutput:
    def __init__(self, target): self.target = target
    def write(self, value):
        kept = value[:remaining[0]]
        remaining[0] -= len(kept)
        if kept: self.target.write(kept)
        return len(value)
    def flush(self): self.target.flush()
sys.stdout, sys.stderr = BoundedOutput(sys.stdout), BoundedOutput(sys.stderr)

async def main():
    scope = {"__name__": "__main__", "call_tool": call_tool, "tools": tools, "asyncio": asyncio}
    tree = ast.parse((root / "code.py").read_text(), "<composition>")
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        tree.body[-1] = ast.copy_location(ast.Assign(
            targets=[ast.Name(id="_composition_result", ctx=ast.Store())],
            value=tree.body[-1].value), tree.body[-1])
        ast.fix_missing_locations(tree)
    result = eval(compile(tree, "<composition>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT), scope)
    if inspect.isawaitable(result): await result
    if scope.get("_composition_result") is not None:
        print(repr(scope["_composition_result"]))

try:
    asyncio.run(main())
except SystemExit:
    raise
except BaseException as error:
    trace = error.__traceback__
    while trace and trace.tb_frame.f_code.co_filename != "<composition>":
        trace = trace.tb_next
    sys.stderr.write("".join(traceback.format_exception(type(error), error, trace)))
    sys.exit(1)
"""


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any]


@dataclass
class CompositionState:
    """In-memory aggregate budgets and completed-effect records for one reply."""

    max_calls: int = MAX_CALLS
    active_timeout: float = ACTIVE_TIMEOUT
    total_timeout: float = TOTAL_TIMEOUT
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started: float | None = None
    count: int = 0
    active: float = 0
    _since: float | None = None
    _waiting: int = 0
    _calls: dict[str, tuple[str, asyncio.Future]] = field(default_factory=dict)
    _run_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _mutations: asyncio.Lock = field(default_factory=asyncio.Lock)
    _reads: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(4))

    def _tick(self) -> None:
        now = time.monotonic()
        if self._since is not None:
            if not self._waiting:
                self.active += now - self._since
            self._since = now

    def begin(self) -> None:
        now = time.monotonic()
        if self.started is None:
            self.started = now
        self._since = now

    def finish(self) -> None:
        self._tick()
        self._since = None

    def expired(self) -> str | None:
        self._tick()
        if (
            self.started is not None
            and time.monotonic() - self.started >= self.total_timeout
        ):
            return "Composition exceeded its overall deadline."
        if self.active >= self.active_timeout:
            return "Composition exceeded its active execution deadline."
        return None

    def remaining(self) -> float:
        self._tick()
        overall = self.total_timeout - (time.monotonic() - self.started)
        active = self.active_timeout - self.active if not self._waiting else overall
        return max(0.001, min(overall, active))


@contextmanager
def approval_wait():
    """Wrap the shared Approver await; no-op outside an active composition call."""
    state = _CLOCK.get()
    if state is not None:
        state._tick()
        state._waiting += 1
    try:
        yield
    finally:
        if state is not None:
            state._tick()
            state._waiting -= 1


class CompositionApprover:
    """Optional adapter for callers whose dispatcher takes its own interaction."""

    def __init__(self, interaction):
        self.interaction = interaction

    async def approve(self, request):
        with approval_wait():
            return await self.interaction.approve(request)


def _failure(error: str) -> dict:
    return {"ok": False, "output": "", "error": error}


def _registry(tools: list[dict]) -> dict[str, dict]:
    registry = {}
    for tool in tools:
        name = tool.get("name")
        if not isinstance(name, str) or name in {"run_code", "compose_tools"}:
            continue
        schema = tool.get(
            "input_schema",
            tool.get("parameters", tool.get("schema", {"type": "object"})),
        )
        registry[name] = {
            "name": name,
            "description": str(tool.get("description", ""))[:2000],
            "input_schema": schema,
            "read_only": tool.get("read_only") is True or tool.get("mutation") is False,
        }
    if len(json.dumps(list(registry.values())).encode()) > COMPOSITION_FRAME_LIMIT:
        raise ValueError("The composition tool registry exceeds its size limit.")
    return registry


def _bounded(result: Any) -> dict:
    if hasattr(result, "model_dump"):
        result = result.model_dump()
    if not isinstance(result, dict):
        return _failure("The tool dispatcher returned an invalid result.")
    try:
        encoded = json.dumps(result, allow_nan=False)
    except (ValueError, TypeError):
        return _failure("The tool dispatcher returned a non-JSON result.")
    if len(encoded.encode()) <= COMPOSITION_FRAME_LIMIT:
        return result
    return {
        "ok": bool(result.get("ok")),
        "output": str(result.get("output", ""))[:MAX_OUTPUT],
        "error": "The tool result exceeded the broker frame limit.",
        "truncated": True,
    }


async def _call(
    frame: dict, registry: dict, state: CompositionState, dispatcher
) -> dict:
    request = Request.model_validate(frame)
    fingerprint = hashlib.sha256(
        json.dumps([request.name, request.arguments], sort_keys=True).encode()
    ).hexdigest()
    if request.id in state._calls:
        previous, pending = state._calls[request.id]
        if previous != fingerprint:
            return _failure(
                "A composition call ID cannot identify different arguments."
            )
        return await asyncio.shield(pending)
    if state.count >= state.max_calls:
        return _failure("The shared composition call budget is exhausted.")
    state.count += 1
    future = asyncio.get_running_loop().create_future()
    state._calls[request.id] = fingerprint, future
    try:
        if request.name not in registry:
            result = _failure(
                "This tool is unavailable to composition; recursive VM calls are forbidden."
            )
        else:
            definition = registry[request.name]
            try:
                jsonschema.Draft202012Validator(definition["input_schema"]).validate(
                    request.arguments
                )
            except jsonschema.ValidationError:
                result = _failure(
                    "The tool arguments do not match its declared schema."
                )
            else:
                gate = state._reads if definition["read_only"] else state._mutations
                async with gate:
                    expired = state.expired()
                    if expired:
                        result = _failure(expired)
                    else:
                        identifier = f"compose:{state.id}:{request.id}"
                        result = _bounded(
                            await dispatcher(
                                request.name, request.arguments, identifier
                            )
                        )
    except asyncio.CancelledError:
        future.set_result(
            _failure(
                "The composed action was cancelled with uncertain effects; it will not be replayed."
            )
        )
        raise
    except Exception:
        result = _failure(
            "The composed action failed with uncertain effects; it will not be replayed automatically."
        )
    future.set_result(result)
    return result


async def compose_tools(
    code: str,
    *,
    dispatcher: Callable[[str, dict, str], Awaitable[dict]],
    tools: list[dict],
    sandbox=None,
    interaction=None,
    state: CompositionState | None = None,
) -> dict:
    """Run Python in a fresh VM, dispatching guest requests through shared tools.

    Preserve ``state`` for a reply. Guest ``call_tool`` accepts a reserved
    ``_call_id`` keyword for safe retries. Approval adapters use ``approval_wait``
    (or ``CompositionApprover``) so only approval waiting pauses the active clock.
    """
    if not isinstance(code, str) or not code.strip() or len(code) > MAX_CODE:
        return _failure("Composition requires 1-20,000 characters of Python.")
    try:
        registry = _registry(tools)
    except (TypeError, ValueError):
        return _failure("Invalid or oversized composition tool registry.")
    state = state if state is not None else CompositionState()
    vm = sandbox if sandbox is not None else CompositionSandbox()
    pending: set[asyncio.Task] = set()
    activity = []
    running = None
    async with state._run_lock:
        state.begin()
        context = _CLOCK.set(state)
        try:
            if expired := state.expired():
                return {**_failure(expired), "activity": activity}
            await asyncio.wait_for(
                vm.prepare(code, list(registry.values()), uuid.uuid4().hex),
                timeout=state.remaining(),
            )
            running = asyncio.create_task(vm.run_composition())

            async def handle(frame):
                result = await _call(frame, registry, state, dispatcher)
                activity.append(
                    {
                        "id": frame["id"],
                        "tool": frame["name"],
                        "ok": bool(result.get("ok")),
                    }
                )
                await vm.write_response(frame["id"], result)

            while not running.done():
                expired = state.expired()
                if expired:
                    return {**_failure(expired), "activity": activity}
                frames = await asyncio.wait_for(
                    vm.poll_requests(), timeout=state.remaining()
                )
                if len(frames) > 5:
                    raise ValueError("The broker batch limit was exceeded.")
                for frame in frames:
                    if (
                        len(json.dumps(frame, allow_nan=False).encode())
                        > COMPOSITION_FRAME_LIMIT
                    ):
                        raise ValueError("The broker frame limit was exceeded.")
                    Request.model_validate(frame)
                    pending.add(asyncio.create_task(handle(frame)))
                finished = {task for task in pending if task.done()}
                for task in finished:
                    task.result()
                pending -= finished
                if len(pending) > MAX_CALLS:
                    raise ValueError("The broker pending call limit was exceeded.")
                await asyncio.sleep(POLL_INTERVAL)
            result = await running
            output = (result.output + ("\n" + result.error if result.error else ""))[
                :MAX_OUTPUT
            ]
            return {
                "ok": result.exit_code == 0 and not result.timed_out,
                "output": output,
                "error": result.error[:MAX_OUTPUT] or None,
                "truncated": len(result.output) + len(result.error) > MAX_OUTPUT,
                "activity": activity,
            }
        except asyncio.CancelledError:
            raise
        except SandboxUnavailable as error:
            return {**_failure(str(error)), "activity": activity}
        except TimeoutError:
            return {
                **_failure(
                    state.expired() or "Composition exceeded its control deadline."
                ),
                "activity": activity,
            }
        except (ValueError, ValidationError):
            return {
                **_failure(
                    "The composition broker received invalid or oversized data."
                ),
                "activity": activity,
            }
        except Exception:
            return {
                **_failure("The composition VM or broker stopped unexpectedly."),
                "activity": activity,
            }
        finally:
            for task in pending:
                task.cancel()
            if running is not None:
                running.cancel()
            await asyncio.gather(
                *pending, *([running] if running else []), return_exceptions=True
            )
            await asyncio.shield(vm.close())
            state.finish()
            _CLOCK.reset(context)
