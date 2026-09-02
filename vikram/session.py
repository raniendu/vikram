"""Supervises session worker subprocesses and fans their events to clients.

One :class:`Session` owns one worker process. Events arrive as NDJSON on the
worker's stdout and are broadcast to every attached listener; commands go back
on its stdin. Nothing here builds or runs an agent -- that happens in the
child, which is the whole point (see ``session_worker`` for why).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from vikram.logging import get_logger

logger = get_logger(__name__)

READY_TIMEOUT_SECONDS = 90.0
STOP_GRACE_SECONDS = 5.0
QUEUE_MAXSIZE = 1000


class SessionError(RuntimeError):
    """Raised when a session cannot be started or addressed."""


@dataclass
class Session:
    id: str
    agent_id: str
    workspace: Path
    process: asyncio.subprocess.Process
    ready: dict[str, Any] = field(default_factory=dict)
    closed: bool = False
    _listeners: set[asyncio.Queue] = field(default_factory=set)
    _pump: asyncio.Task | None = None
    _ready_event: asyncio.Event = field(default_factory=asyncio.Event)
    _failure: str | None = None
    _backlog: list[dict[str, Any]] = field(default_factory=list)

    # --- fan-out -------------------------------------------------------

    def attach(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        for event in self._backlog:
            queue.put_nowait(event)
        self._listeners.add(queue)
        return queue

    def detach(self, queue: asyncio.Queue) -> None:
        self._listeners.discard(queue)

    def _broadcast(self, event: dict[str, Any]) -> None:
        # Kept so a client attaching after the first turn still sees what the
        # session is: without it the UI would have no model or tool list.
        if event.get("type") in {"session.ready", "session.closed"}:
            self._backlog.append(event)
        for queue in list(self._listeners):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("session_listener_slow", session=self.id)
                self._listeners.discard(queue)

    async def _pump_stdout(self) -> None:
        assert self.process.stdout is not None
        while True:
            line = await self.process.stdout.readline()
            if not line:
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("session_unparseable_event", session=self.id)
                continue
            if event.get("type") == "session.ready":
                self.ready = event
                self._ready_event.set()
            elif event.get("type") == "session.failed":
                self._failure = str(event.get("error"))
                self._ready_event.set()
            self._broadcast(event)

        self.closed = True
        self._ready_event.set()
        self._broadcast(
            {"type": "session.closed", "session_id": self.id, "payload": {}}
        )

    async def wait_ready(self) -> None:
        try:
            await asyncio.wait_for(self._ready_event.wait(), READY_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            await self.stop()
            raise SessionError("Session worker did not become ready.") from exc
        if self._failure:
            await self.stop()
            raise SessionError(self._failure)
        if self.closed:
            raise SessionError("Session worker exited before becoming ready.")

    # --- commands ------------------------------------------------------

    async def send(self, message: dict[str, Any]) -> None:
        if self.closed or self.process.stdin is None:
            raise SessionError(f"Session {self.id} is closed.")
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def prompt(self, text: str) -> str:
        turn_id = uuid.uuid4().hex[:12]
        await self.send({"cmd": "prompt", "turn_id": turn_id, "prompt": text})
        return turn_id

    async def approve(
        self, approval_id: str, decision: str, *, tool_name: str | None = None
    ) -> None:
        await self.send(
            {
                "cmd": "approve",
                "approval_id": approval_id,
                "decision": decision,
                "tool_name": tool_name or "",
            }
        )

    async def cancel(self) -> None:
        await self.send({"cmd": "cancel"})

    async def set_flags(self, *, approve_all: bool) -> None:
        await self.send({"cmd": "set_flags", "approve_all": approve_all})

    # --- teardown ------------------------------------------------------

    async def stop(self) -> None:
        """Terminate the worker and everything it spawned.

        Killing the process *group* is what reaps ``run_command`` children and
        MCP stdio servers; cancelling an asyncio task would leave them behind.
        """
        if self.process.returncode is not None:
            self.closed = True
            return
        with contextlib.suppress(ProcessLookupError):
            await self.send({"cmd": "shutdown"})
        try:
            await asyncio.wait_for(self.process.wait(), STOP_GRACE_SECONDS)
        except (asyncio.TimeoutError, Exception):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
            with contextlib.suppress(Exception):
                await self.process.wait()
        self.closed = True
        if self._pump is not None:
            self._pump.cancel()
        logger.info("session_stopped", session=self.id, agent=self.agent_id)


class SessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def get(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    def list(self) -> list[Session]:
        return list(self._sessions.values())

    async def create(self, *, agent_id: str, workspace: Path) -> Session:
        workspace = workspace.expanduser().resolve()
        if not workspace.is_dir():
            raise SessionError(f"Workspace is not a directory: {workspace}")

        session_id = uuid.uuid4().hex[:12]
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "vikram.session_worker",
            "--session-id",
            session_id,
            "--agent",
            agent_id,
            "--workspace",
            str(workspace),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,
            # Its own process group, so stop() can reap the whole tree.
            start_new_session=True,
        )
        session = Session(
            id=session_id, agent_id=agent_id, workspace=workspace, process=process
        )
        session._pump = asyncio.create_task(session._pump_stdout())
        self._sessions[session_id] = session
        try:
            await session.wait_ready()
        except SessionError:
            self._sessions.pop(session_id, None)
            raise
        logger.info(
            "session_started",
            session=session_id,
            agent=agent_id,
            workspace=str(workspace),
            pid=process.pid,
        )
        return session

    async def close(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            await session.stop()

    async def close_all(self) -> None:
        for session_id in list(self._sessions):
            await self.close(session_id)


async def sse_stream(session: Session) -> AsyncIterator[str]:
    """Serialise a session's events as Server-Sent Events.

    A heartbeat comment keeps intermediaries from closing an idle connection
    while the user is reading rather than typing.
    """
    queue = session.attach()
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15.0)
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
                if session.closed:
                    return
                continue
            name = event.get("type", "message")
            seq = event.get("seq")
            prefix = f"id: {seq}\n" if seq is not None else ""
            yield f"{prefix}event: {name}\ndata: {json.dumps(event)}\n\n"
            if name == "session.closed":
                return
    finally:
        session.detach(queue)


__all__ = [
    "Session",
    "SessionError",
    "SessionRegistry",
    "sse_stream",
]
