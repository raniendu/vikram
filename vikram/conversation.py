"""One streaming model/tool loop for every interactive Vikram interface."""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable

import httpx
from pydantic_ai import AgentRunResultEvent
from pydantic_ai.messages import (
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
)
from pydantic_ai.usage import UsageLimits

from vikram.agent import build_agent
from vikram.browser import BrowserEngine
from vikram.errors import ReplyError, reply_error
from vikram.interaction import Interaction, Unattended
from vikram.ollama import thinking_request
from vikram.sandbox import Sandbox, host_sandbox
from vikram.schedules import TaskBook
from vikram.toolkit import REQUEST_LIMIT, Mode, Reply
from vikram.web import WebSession

DEFAULT_WEB = "auto"
MAX_REASONING_CHARS = 64_000


class ObservedStream(httpx.AsyncByteStream):
    def __init__(self, source, reported: dict):
        self.source, self.reported = source, reported

    async def __aiter__(self):
        buffer = b""
        async for chunk in self.source:
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if line.startswith(b"data: "):
                    try:
                        payload = json.loads(line[6:])
                        if isinstance(payload.get("model"), str) and payload["model"]:
                            self.reported["model"] = payload["model"]
                    except (ValueError, AttributeError):
                        pass
            if len(buffer) > 1024 * 1024:
                buffer = b""
            yield chunk

    async def aclose(self):
        await self.source.aclose()


async def stream_reply(
    model,
    thinking,
    history,
    prompt,
    *,
    web="off",
    session_id="",
    transport=None,
    mode: Mode = "normal",
    interaction: Interaction | None = None,
    sandbox: Callable[[], Sandbox | None] | None = None,
    browser: BrowserEngine | None = None,
    vision: bool = False,
    tasks: TaskBook | None = None,
    scheduled: str | None = None,
) -> AsyncIterator[dict]:
    """Stream one reply. Tool-capable models (web "auto") also get the toolkit.

    `interaction` is the person who answers questions, reviews plans, and
    approves browser actions; without one, questions are declined, plans stay
    unapproved, and consequential browser actions are declined. `sandbox` makes
    this reply's code sandbox, by default a new microVM when this host can run
    one; the reply deletes it when it ends. `browser` is the interface's browser
    engine, which outlives the reply; `vision` adds screenshots to its pages.
    `tasks` lets the model manage scheduled tasks. `scheduled` marks an
    unattended scheduled run of that task: no questions, browser, or task tools.
    """
    queue = asyncio.Queue()
    reported, parts = {}, {}
    current = ""
    reasoning = ""
    reasoning_truncated = False
    reasoning_boundary = False

    def append_reasoning(content: str, *, new_part=False):
        nonlocal reasoning, reasoning_truncated, reasoning_boundary
        if new_part:
            reasoning_boundary = bool(reasoning)
        if not content:
            return
        if reasoning_boundary:
            content = "\n\n" + content
            reasoning_boundary = False
        remaining = MAX_REASONING_CHARS - len(reasoning)
        reasoning += content[:remaining]
        reasoning_truncated |= len(content) > remaining

    def send(phase="answering", **extra):
        queue.put_nowait(
            {
                "text": current,
                "reasoning": reasoning,
                "reasoning_truncated": reasoning_truncated,
                "actual_model": reported.get("model"),
                "phase": phase,
                "mode": reply.mode,
                "activity": reply.snapshot(),
                **session.state(),
                **extra,
            }
        )

    session = WebSession(session_id, emit=lambda event: send(**event))
    reply = Reply(
        mode=mode,
        interaction=interaction or Unattended(),
        notify=lambda phase: send(phase=phase),
        # The browser needs tool calling, like the rest of the toolkit.
        browser=browser if web == "auto" and not scheduled else None,
        vision=vision,
        tasks=tasks if web == "auto" and not scheduled else None,
        scheduled=scheduled,
    )

    async def observe(response):
        response.stream = ObservedStream(response.stream, reported)

    async def produce():
        nonlocal current
        try:
            if web == "auto":
                reply.sandbox = (sandbox or host_sandbox)()
            async with httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(300, connect=5),
                transport=transport,
                event_hooks={"response": [observe]},
            ) as client:
                agent = build_agent(
                    model,
                    http_client=client,
                    web=web,
                    tools=session.tools() if web == "auto" else (),
                    toolkit=web == "auto",
                    sandbox=reply.sandbox is not None,
                )
                agent.model.client.max_retries = 0
                async with agent.run_stream_events(
                    prompt,
                    deps=reply,
                    message_history=history,
                    model_settings={"extra_body": thinking_request(thinking)},
                    usage_limits=UsageLimits(request_limit=REQUEST_LIMIT),
                ) as events:
                    async for event in events:
                        if isinstance(event, PartStartEvent):
                            if event.index == 0:
                                parts.clear()
                            if isinstance(event.part, TextPart):
                                parts[event.index] = event.part.content
                                current = "".join(parts.values())
                                send()
                            elif isinstance(event.part, ThinkingPart):
                                append_reasoning(event.part.content, new_part=True)
                                send(phase="thinking")
                        elif isinstance(event, PartDeltaEvent) and isinstance(
                            event.delta, TextPartDelta
                        ):
                            parts[event.index] = (
                                parts.get(event.index, "") + event.delta.content_delta
                            )
                            current = "".join(parts.values())
                            send()
                        elif isinstance(event, PartDeltaEvent) and isinstance(
                            event.delta, ThinkingPartDelta
                        ):
                            append_reasoning(event.delta.content_delta or "")
                            send(phase="thinking")
                        elif isinstance(event, FunctionToolResultEvent):
                            parts.clear()
                        elif isinstance(event, AgentRunResultEvent):
                            current = event.result.output
                            send()
        except Exception as exc:
            queue.put_nowait(ReplyError(reply_error(exc)))
        finally:
            if reply.sandbox is not None:
                # Shielded: a second stop must not leave the VM running.
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await asyncio.shield(reply.sandbox.close())
            queue.put_nowait(None)

    task = asyncio.create_task(produce())
    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            if isinstance(event, Exception):
                raise event
            yield event
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
