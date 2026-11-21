"""One streaming model/tool loop for every interactive Vikram interface."""

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any, cast

import httpx
from pydantic_ai import AgentRunResultEvent
from pydantic_ai.messages import (
    FunctionToolResultEvent,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
)
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from vikram.agent import build_agent
from vikram.browser import BrowserEngine
from vikram.errors import ReplyError, has_tool_call_text, reply_error
from vikram.interaction import Interaction, Unattended
from vikram.providers import ModelRef, model_settings
from vikram.sandbox import Sandbox, host_sandbox
from vikram.schedules import TaskBook
from vikram.toolkit import REQUEST_LIMIT, Mode, Reply
from vikram.web import WebSession

DEFAULT_WEB = "auto"
MAX_REASONING_CHARS = 64_000
STREAM_UPDATE_SECONDS = 0.1


def context_tokens(messages) -> int | None:
    """Tokens in the latest request's prompt plus its response, as reported."""
    response = next(
        (m for m in reversed(messages) if isinstance(m, ModelResponse)), None
    )
    if response is None or response.usage.input_tokens <= 0:
        return None
    return response.usage.input_tokens + response.usage.output_tokens


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
    sandbox_state: Path | None = None,
    browser: BrowserEngine | None = None,
    vision: bool = False,
    tasks: TaskBook | None = None,
    scheduled: str | None = None,
    coding=None,
    instructions: str = "",
    request_boundary=None,
    usage_limits: UsageLimits | None = None,
    web_tools: frozenset[str] | None = None,
) -> AsyncGenerator[dict, None]:
    """Stream one reply. Tool-capable models (web "auto") also get the toolkit.

    `interaction` is the person who answers questions, reviews plans, and
    approves browser actions; without one, questions are declined, plans stay
    unapproved, and consequential browser actions are declined. `sandbox` makes
    this reply's code sandbox. A private `sandbox_state` path reattaches to a saved
    Pydantic workspace and stops/flushes it at reply end; without one the VM is
    disposable. Scheduled runs never inherit persistence. `browser` is the interface's browser
    engine, which outlives the reply; `vision` adds screenshots to its pages.
    `tasks` lets the model manage scheduled tasks. `scheduled` marks an
    unattended scheduled run of that task: no questions, browser, or task tools.
    `web_tools` limits the web tools offered (`web_search`, `web_fetch`).
    """
    queue = asyncio.Queue()
    reported, parts = {}, {}
    current = ""
    reasoning = ""
    reasoning_truncated = False
    reasoning_boundary = False
    loop = asyncio.get_running_loop()
    next_update = 0.0
    last_phase = ""
    pending_update: asyncio.TimerHandle | None = None

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
        nonlocal pending_update, next_update, last_phase
        if pending_update is not None:
            pending_update.cancel()
            pending_update = None
        last_phase = phase
        next_update = loop.time() + STREAM_UPDATE_SECONDS
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

    def stream_update(phase):
        nonlocal pending_update
        # Both interfaces checkpoint full snapshots. Batch tokens, while
        # delivering the first token, phase changes and tool activity immediately.
        if phase != last_phase or loop.time() >= next_update:
            send(phase)
        elif pending_update is None:
            pending_update = loop.call_at(next_update, send, phase)

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
        coding=coding if not scheduled else None,
        instructions=instructions,
        request_boundary=request_boundary,
        sandbox_persistent=sandbox_state is not None and not scheduled,
    )

    async def observe(response):
        response.stream = ObservedStream(response.stream, reported)

    async def produce():
        nonlocal current
        failure = None
        try:
            if web == "auto":
                reply.sandbox = (
                    sandbox()
                    if sandbox is not None
                    else host_sandbox(
                        state_path=sandbox_state if not scheduled else None
                    )
                )
            async with httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(300, connect=5),
                transport=transport,
                event_hooks={"response": [observe]},
            ) as client:
                from vikram.execution import execution_tools
                from vikram.integrations import lifecycle
                from vikram.toolkit import toolkit_tools

                offered = [
                    tool
                    for tool in (session.tools() if web == "auto" else [])
                    if web_tools is None or tool.name in web_tools
                ]
                host_tools = (
                    await execution_tools(
                        reply,
                        builtin_tools=[
                            *offered,
                            *toolkit_tools(reply.sandbox is not None),
                        ],
                    )
                    if web == "auto"
                    else None
                )
                if (
                    coding is not None
                    and not scheduled
                    and hasattr(coding, "extensions")
                ):
                    await lifecycle(
                        reply, "before_reply", {"model": model, "prompt": prompt}
                    )
                agent = build_agent(
                    model,
                    http_client=client,
                    web=web,
                    tools=offered,
                    toolsets=[host_tools] if host_tools is not None else [],
                    toolkit=web == "auto",
                    sandbox=reply.sandbox is not None,
                )
                async with agent.run_stream_events(
                    prompt,
                    deps=reply,
                    message_history=history,
                    model_settings=cast(ModelSettings, model_settings(model, thinking)),
                    usage_limits=usage_limits
                    or UsageLimits(request_limit=REQUEST_LIMIT),
                ) as events:
                    async for event in events:
                        if isinstance(event, PartStartEvent):
                            if event.index == 0:
                                parts.clear()
                            if isinstance(event.part, TextPart):
                                parts[event.index] = event.part.content
                                current = "".join(parts.values())
                                stream_update("answering")
                            elif isinstance(event.part, ThinkingPart):
                                append_reasoning(event.part.content, new_part=True)
                                stream_update("thinking")
                        elif isinstance(event, PartDeltaEvent) and isinstance(
                            event.delta, TextPartDelta
                        ):
                            parts[event.index] = (
                                parts.get(event.index, "") + event.delta.content_delta
                            )
                            current = "".join(parts.values())
                            stream_update("answering")
                        elif isinstance(event, PartDeltaEvent) and isinstance(
                            event.delta, ThinkingPartDelta
                        ):
                            append_reasoning(event.delta.content_delta or "")
                            stream_update("thinking")
                        elif isinstance(event, FunctionToolResultEvent):
                            parts.clear()
                        elif isinstance(event, AgentRunResultEvent):
                            current = event.result.output
                            usage = event.result.usage
                            counts: dict[str, Any] = {
                                "input_tokens": usage.input_tokens,
                                "output_tokens": usage.output_tokens,
                                "requests": usage.requests,
                                "cache_read_tokens": usage.cache_read_tokens,
                            }
                            # The run's totals add up every request; the latest
                            # request's prompt and output are what the context holds.
                            if tokens := context_tokens(event.result.new_messages()):
                                counts["context_tokens"] = tokens
                            if ModelRef.parse(model).is_cloud:
                                reported_costs = [
                                    (message.provider_details or {}).get("cost")
                                    for message in event.result.new_messages()
                                    if isinstance(message, ModelResponse)
                                ]
                                if reported_costs and all(
                                    isinstance(cost, (int, float))
                                    for cost in reported_costs
                                ):
                                    counts.update(
                                        cost_usd=str(
                                            sum(
                                                cost
                                                for cost in reported_costs
                                                if isinstance(cost, (int, float))
                                            )
                                        ),
                                        cost_estimated=False,
                                    )
                                elif getattr(usage, "cost", None) is not None:
                                    counts.update(
                                        cost_usd=str(usage.cost), cost_estimated=True
                                    )
                            if (
                                coding is not None
                                and not scheduled
                                and hasattr(coding, "extensions")
                            ):
                                await lifecycle(
                                    reply,
                                    "after_reply",
                                    {"model": model, "text": current, "usage": counts},
                                )
                            send(usage=counts)
        except Exception as exc:
            if has_tool_call_text(current):
                # Do not persist unexecuted tool syntax as a usable partial answer.
                current = ""
            send()
            error = ReplyError(
                reply_error(exc, provider=ModelRef.parse(model).provider)
            )
            # Preserve the typed cause for unattended budget handling without
            # exposing provider exception text in the transcript.
            error.__cause__ = exc
            failure = error
        finally:
            if pending_update is not None:
                send(last_phase)
            if reply.sandbox is not None:
                # Do not release chat ownership while a second Stop cancels the
                # wait: Smol must finish stopping/flushing the persistent disk.
                closing = asyncio.create_task(reply.sandbox.close())
                while not closing.done():
                    try:
                        await asyncio.shield(closing)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                try:
                    closing.result()
                except (Exception, asyncio.CancelledError):
                    if reply.sandbox_persistent and failure is None:
                        failure = ReplyError(
                            "The conversation sandbox could not be safely saved. "
                            "Its reference was retained; recent writes are uncertain. "
                            "Inspect the files before continuing."
                        )
            if failure is not None:
                queue.put_nowait(failure)
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
