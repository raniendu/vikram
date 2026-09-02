"""One agent session, in its own process, speaking NDJSON over stdio.

Two pieces of runtime state are process-global and both are load-bearing:
``set_command_policy`` writes a module-level policy (``tools.py``), and the
workspace root is ``Path.cwd()``. So one process can safely host exactly one
(agent, workspace) pair, and the unit of isolation is a process.

Threading an explicit root through ``_resolve_workspace_path`` and
``_is_sensitive_path`` instead would mean refactoring precisely the code that
prevents path escape and secret reads -- the wrong place to take a risk for
GUI convenience. A process also gives clean teardown: killing the process
group reaps ``run_command`` children and MCP stdio servers deterministically.

**stdout is the protocol.** All logging goes to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from vikram.agent import ApprovalRequest
from vikram.events import Event, map_stream_event
from vikram.logging import configure_logging, get_logger

logger = get_logger(__name__)

APPROVAL_TIMEOUT_SECONDS = 300.0


class SessionWorker:
    def __init__(self, *, session_id: str, agent_id: str, workspace: Path) -> None:
        self.session_id = session_id
        self.agent_id = agent_id
        self.workspace = workspace
        self.agent: Any = None
        self.approve_all = False
        self.always_allow: set[str] = set()
        self._seq = 0
        self._turn: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[str]] = {}
        self._approval_n = 0
        self._history: list[Any] = []
        self._out_lock = asyncio.Lock()

    # --- output --------------------------------------------------------

    async def emit(self, event: Event) -> None:
        async with self._out_lock:
            sys.stdout.write(event.model_dump_json() + "\n")
            sys.stdout.flush()

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _event(self, type_: str, payload: dict[str, Any], **kw: Any) -> Event:
        return Event(
            type=type_,  # type: ignore[arg-type]
            seq=self._next_seq(),
            session_id=self.session_id,
            payload=payload,
            **kw,
        )

    # --- approvals -----------------------------------------------------

    async def _ask_approval(self, request: ApprovalRequest) -> str:
        """Surface an approval to the client and wait for its answer.

        Auto-approvals still round-trip through here so the transcript is a
        complete audit trail: the client sees every approval that was granted,
        including the ones it never had to answer.
        """
        self._approval_n += 1
        approval_id = f"{self.session_id}-a{self._approval_n}"
        auto = self.approve_all or request.tool_name in self.always_allow

        await self.emit(
            self._event(
                "approval.requested",
                {
                    "approval_id": approval_id,
                    "tool_name": request.tool_name,
                    "tool_call_id": request.tool_call_id,
                    "input": request.args,
                    "rendered": str(request),
                    "auto": auto,
                },
            )
        )

        if auto:
            await self.emit(
                self._event(
                    "approval.resolved",
                    {"approval_id": approval_id, "decision": "auto"},
                )
            )
            return "yes"

        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending[approval_id] = future
        try:
            answer = await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT_SECONDS)
            decision = "allow" if answer == "yes" else "deny"
        except asyncio.TimeoutError:
            # A closed window would otherwise leave this awaiting forever while
            # holding a `sequential` tool lock.
            answer, decision = "no", "timeout"
        finally:
            self._pending.pop(approval_id, None)

        await self.emit(
            self._event(
                "approval.resolved",
                {"approval_id": approval_id, "decision": decision},
            )
        )
        return answer

    def resolve_approval(self, approval_id: str, decision: str) -> None:
        future = self._pending.get(approval_id)
        if future is None or future.done():
            logger.warning("approval_unknown_or_settled", approval_id=approval_id)
            return
        if decision == "allow_always":
            future.set_result("yes")
            return
        future.set_result("yes" if decision == "allow" else "no")

    # --- lifecycle -----------------------------------------------------

    async def start(self) -> None:
        from vikram.agent import build_agent
        from vikram.settings import VikramSettings
        from vikram.spec import ensure_surface_allowed
        from vikram.specstore import load_agent

        os.chdir(self.workspace)
        settings = VikramSettings()
        spec = load_agent(self.agent_id, settings)
        ensure_surface_allowed(spec, "gui")
        self.agent = build_agent(
            spec=spec,
            settings=settings,
            surface="gui",
            approve_all=False,
            approval_request=self._ask_approval,
        )

    async def ready_event(self) -> Event:
        return self._event(
            "session.ready",
            {
                "agent_id": self.agent_id,
                "name": self.agent.name,
                # The real cwd, not the requested path: this is what the
                # workspace guards in tools.py actually resolve against.
                "workspace": os.getcwd(),
                "model_config": dict(self.agent.model_config),
                "tool_names": list(self.agent.tool_names),
                "approval_tool_names": list(self.agent.approval_tool_names),
                "mcp_servers": [client.id for client in self.agent.mcp_clients],
            },
        )

    # --- turns ---------------------------------------------------------

    async def run_turn(self, turn_id: str, prompt: str) -> None:
        started = time.perf_counter()
        ttft_ms: float | None = None
        await self.emit(
            self._event("turn.started", {"prompt_length": len(prompt)}, turn_id=turn_id)
        )
        try:
            async for raw in self.agent.stream_events(
                prompt,
                message_history=self._history or None,
                conversation_id=f"gui:{self.session_id}",
            ):
                result = raw.get("vikram_result")
                if result is not None:
                    self._history = list(result.all_messages())
                    await self.emit(
                        self._event(
                            "turn.finished",
                            {
                                "output": str(result.output),
                                "usage": _usage(result),
                                "duration_ms": _ms(started),
                                "ttft_ms": ttft_ms,
                            },
                            turn_id=turn_id,
                        )
                    )
                    return
                event = map_stream_event(
                    raw,
                    seq=self._next_seq(),
                    session_id=self.session_id,
                    turn_id=turn_id,
                )
                if event is None:
                    continue
                if ttft_ms is None and event.type in {"text.delta", "thinking.delta"}:
                    ttft_ms = _ms(started)
                await self.emit(event)
        except asyncio.CancelledError:
            await self.emit(
                self._event(
                    "turn.cancelled", {"duration_ms": _ms(started)}, turn_id=turn_id
                )
            )
            raise
        except Exception as exc:
            logger.exception("turn_failed", turn_id=turn_id)
            await self.emit(
                self._event(
                    "turn.failed",
                    {"error": str(exc), "error_type": type(exc).__name__},
                    turn_id=turn_id,
                )
            )

    def cancel_turn(self) -> None:
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()

    # --- command loop --------------------------------------------------

    async def handle(self, message: dict[str, Any]) -> bool:
        """Apply one command. Returns False when the worker should stop."""
        command = message.get("cmd")
        if command == "prompt":
            self.cancel_turn()
            self._turn = asyncio.create_task(
                self.run_turn(message["turn_id"], message["prompt"])
            )
        elif command == "approve":
            self.resolve_approval(
                message["approval_id"], message.get("decision", "deny")
            )
            if message.get("decision") == "allow_always":
                self.always_allow.add(message.get("tool_name", ""))
        elif command == "set_flags":
            if "approve_all" in message:
                self.approve_all = bool(message["approve_all"])
        elif command == "cancel":
            self.cancel_turn()
        elif command == "shutdown":
            self.cancel_turn()
            return False
        else:
            logger.warning("unknown_command", cmd=command)
        return True

    async def serve(self) -> None:
        await self.start()
        # Holding the agent's context for the session keeps MCP stdio servers
        # warm; otherwise every turn respawns them, each with a 5s init timeout.
        async with self.agent:
            await self.emit(await self.ready_event())
            await self._read_commands()
        await self.emit(self._event("session.closed", {}))

    async def _read_commands(self) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
        )
        while True:
            line = await reader.readline()
            if not line:
                self.cancel_turn()
                return
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("unparseable_command")
                continue
            if not await self.handle(message):
                return


class PlaygroundWorker(SessionWorker):
    """Runs one prompt against several models inside a single process.

    Safe because the agent and the workspace are constant across columns, so
    the process-global command policy and cwd are identical for all of them --
    see vikram/playground.py.
    """

    def __init__(self, *, columns: list[Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.columns = columns
        self.agents: list[Any] = []

    async def start(self) -> None:
        from vikram.playground import build_column_agents
        from vikram.settings import VikramSettings
        from vikram.spec import ensure_surface_allowed
        from vikram.specstore import load_agent

        os.chdir(self.workspace)
        settings = VikramSettings()
        spec = load_agent(self.agent_id, settings)
        ensure_surface_allowed(spec, "gui")
        self.agents = build_column_agents(spec, settings, self.columns)
        # The first agent stands in for shared facts (tools, prompt); models
        # differ per column and are reported alongside each column.
        self.agent = self.agents[0]

    async def ready_event(self) -> Event:
        return self._event(
            "session.ready",
            {
                "agent_id": self.agent_id,
                "name": self.agent.name,
                "workspace": os.getcwd(),
                "columns": [
                    {"column_id": c.id, "provider": c.provider, "model": c.model}
                    for c in self.columns
                ],
                "tool_names": list(self.agent.tool_names),
                "approval_tool_names": list(self.agent.approval_tool_names),
                "approvals_disabled": bool(self.agent.approval_tool_names),
            },
        )

    async def run_turn(self, turn_id: str, prompt: str) -> None:
        from vikram.playground import run_comparison

        async def emit(type_: str, payload: dict[str, Any], column_id: str) -> None:
            await self.emit(
                self._event(type_, payload, turn_id=turn_id, column_id=column_id)
            )

        await self.emit(
            self._event("turn.started", {"prompt_length": len(prompt)}, turn_id=turn_id)
        )
        try:
            metrics = await run_comparison(self.agents, self.columns, prompt, emit=emit)
        except asyncio.CancelledError:
            await self.emit(self._event("turn.cancelled", {}, turn_id=turn_id))
            raise
        from dataclasses import asdict as _asdict

        await self.emit(
            self._event(
                "turn.finished",
                {"columns": [_asdict(m) for m in metrics]},
                turn_id=turn_id,
            )
        )


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _usage(result: Any) -> dict[str, int | None]:
    """Token counts, read defensively.

    pydantic-ai has moved these field names between versions; ``cli.py`` uses
    the same guarded shape when computing context usage.
    """
    try:
        # `usage` is a property in current pydantic-ai and was a method in
        # earlier ones; accept either rather than pinning to one release.
        usage = result.usage
        if callable(usage):
            usage = usage()
    except Exception as exc:
        # Reported rather than swallowed: silently showing None for every token
        # count would make the playground's model comparison useless and give
        # no hint why.
        logger.warning(
            "usage_unavailable", error=str(exc), error_type=type(exc).__name__
        )
        return {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    return {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vikram-session",
        description="Run one agent session, speaking NDJSON over stdio.",
    )
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument(
        "--models",
        default=None,
        help="Comma-separated provider/model pairs; enables playground mode.",
    )
    args = parser.parse_args(argv)

    # stdout is the protocol.
    configure_logging(stream=sys.stderr)

    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        print(
            json.dumps(
                {"type": "session.failed", "error": f"No such directory: {workspace}"}
            ),
            flush=True,
        )
        return 1

    if args.models:
        from vikram.playground import ColumnSpec, PlaygroundError, validate_columns

        columns = []
        for entry in args.models.split(","):
            provider, _, model = entry.strip().partition("/")
            if not provider or not model:
                print(
                    json.dumps(
                        {
                            "type": "session.failed",
                            "error": f"Malformed column {entry!r}; expected provider/model.",
                        }
                    ),
                    flush=True,
                )
                return 1
            columns.append(ColumnSpec(provider=provider, model=model))
        try:
            validate_columns(columns)
        except PlaygroundError as exc:
            print(json.dumps({"type": "session.failed", "error": str(exc)}), flush=True)
            return 1
        worker: SessionWorker = PlaygroundWorker(
            session_id=args.session_id,
            agent_id=args.agent,
            workspace=workspace,
            columns=columns,
        )
    else:
        worker = SessionWorker(
            session_id=args.session_id, agent_id=args.agent, workspace=workspace
        )
    try:
        asyncio.run(worker.serve())
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logger.exception("session_worker_failed")
        print(
            json.dumps({"type": "session.failed", "error": str(exc)}),
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
