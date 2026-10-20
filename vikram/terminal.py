"""An inline terminal interface; model execution stays in ChatSession."""

import asyncio
import contextlib
import re
import time
import warnings
from typing import TYPE_CHECKING

from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import Input, create_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.styles import Style
from pydantic_ai import PydanticAIDeprecationWarning
from pydantic_ai.exceptions import AgentRunError
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

from vikram.errors import ReplyError, reply_error
from vikram.model_names import format_model_name

if TYPE_CHECKING:
    from vikram.cli import ChatSession

ACCENT = "#cf884d"
ESCAPES = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
CONTROLS = dict.fromkeys([*range(0, 9), *range(11, 32), *range(127, 160)])


def terminal_text(value: str) -> str:
    """Keep model/tool text from supplying terminal control sequences."""
    return ESCAPES.sub("", value).translate(CONTROLS)


def render_reply(
    event: dict,
    *,
    show_thinking: bool = False,
    elapsed: float = 0,
    status: str = "running",
) -> Group:
    items = []
    reasoning = terminal_text(event.get("reasoning", ""))
    if reasoning:
        if show_thinking:
            # A short tail leaves room for the answer even during long reasoning.
            tail = "\n".join(reasoning.splitlines()[-8:])[-1200:]
            items.append(
                Panel(
                    Text(tail, style="dim"),
                    title="Thinking · latest output",
                    title_align="left",
                    border_style="dim",
                    padding=(0, 1),
                )
            )
        elif event.get("phase") != "thinking" or status != "running":
            hint = "Ctrl+T to view" if status == "running" else "/trace to view"
            items.append(Text(f"› Thinking · {hint}", style="dim"))
        if show_thinking and event.get("reasoning_truncated"):
            items.append(Text("Thinking reached the display limit.", style="dim"))
    answer = terminal_text(event.get("text", ""))
    if answer:
        items.append(Markdown(answer, hyperlinks=False, code_theme="ansi_dark"))
    if status == "running":
        phase = {
            "thinking": "Thinking…",
            "searching": "Searching the web…",
            "fetching": "Reading sources…",
            "answering": "Writing…",
        }.get(event.get("phase"), "Waiting for Ollama…")
        hint = " · Ctrl+T " + ("hide" if show_thinking else "view") if reasoning else ""
        items.append(
            Text(f"● {phase} {elapsed:.0f}s{hint} · Ctrl+C stop", style=ACCENT)
        )
    else:
        items.append(Text(f"{status.capitalize()} · {elapsed:.1f}s", style="dim"))
    return Group(*items)


class _LiveReply:
    def __init__(self, terminal: "TerminalChat", started: float):
        self.terminal, self.started = terminal, started

    def __rich_console__(self, console, options):
        yield render_reply(
            self.terminal.last_event,
            show_thinking=self.terminal.show_thinking,
            elapsed=time.monotonic() - self.started,
        )


class TerminalChat:
    def __init__(
        self,
        session: "ChatSession",
        *,
        console: Console | None = None,
        terminal_input: Input | None = None,
    ):
        self.session = session
        self.console = console or Console()
        self.input = terminal_input
        self.last_event: dict = {}
        self.show_thinking = False

    def show_trace(self) -> None:
        reasoning = self.last_event.get("reasoning", "")
        if not reasoning:
            self.console.print(
                "No thinking was returned for the latest reply.", style="dim"
            )
            return
        self.console.print(Rule("Thinking · most recent reply", style="dim"))
        self.console.print(Text(terminal_text(reasoning), style="dim"))
        if self.last_event.get("reasoning_truncated"):
            self.console.print("Thinking reached the display limit.", style="dim")
        self.console.print(Rule(style="dim"))

    def show_activity(self) -> None:
        searches = self.last_event.get("searches", [])
        if not searches:
            self.console.print("No web requests in the latest reply.", style="dim")
        for search in searches:
            arguments = search["arguments"]
            query = ", ".join(
                arguments.get("search_queries", arguments.get("urls", []))
            )
            label = "Search" if search["tool"] == "web_search" else "Read"
            self.console.print(
                Text(
                    terminal_text(f"{label} · {search['status']} · {query}"),
                    style="dim",
                )
            )
            if search.get("error"):
                self.console.print(Text(terminal_text(search["error"]), style="dim"))

    async def answer(self, message: str) -> None:
        self.last_event = {"phase": "loading"}
        self.show_thinking = False
        started = time.monotonic()
        status, error, notices = "complete", None, []
        stopped = False
        self.console.print()
        self.console.print(
            Text.assemble(
                ("v. ", f"bold {ACCENT}"),
                ("Vikram", "bold"),
                (f" · {terminal_text(format_model_name(self.session.model))}", "dim"),
            )
        )

        async def consume():
            async with contextlib.aclosing(self.session.reply(message)) as events:
                async for event in events:
                    if stopped:
                        raise asyncio.CancelledError
                    if event.get("notice"):
                        notices.append(event["notice"])
                    else:
                        self.last_event = event

        task = asyncio.create_task(consume())

        def keys_ready():
            nonlocal stopped
            for key in self.input.read_keys():
                if key.key == Keys.ControlT:
                    self.show_thinking = not self.show_thinking
                elif key.key in (Keys.ControlC, Keys.ControlD):
                    stopped = True
                    task.cancel()

        try:
            with (
                Live(
                    _LiveReply(self, started),
                    console=self.console,
                    refresh_per_second=8,
                    transient=True,
                ),
                self.input.raw_mode() if self.input else contextlib.nullcontext(),
                (
                    self.input.attach(keys_ready)
                    if self.input
                    else contextlib.nullcontext()
                ),
            ):
                await task
        except asyncio.CancelledError:
            status = "stopped"
            if asyncio.current_task().cancelling():
                raise
        except (ReplyError, AgentRunError, ValueError) as exc:
            status = "failed"
            error = str(exc) if isinstance(exc, ValueError) else reply_error(exc)
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self.console.print(
            render_reply(
                self.last_event, elapsed=time.monotonic() - started, status=status
            )
        )
        for notice in notices:
            self.console.print(Text(terminal_text(notice), style="dim"))
        searches = self.last_event.get("searches", [])
        if searches:
            self.console.print(
                f"Web · {len(searches)} requests · /activity to inspect", style="dim"
            )
        for source in self.last_event.get("sources", []):
            self.console.print(
                Text(
                    terminal_text(
                        f"[{source['id']}] {source['title']} · {source['url']}"
                    ),
                    style="dim",
                )
            )
        if any(s["status"] == "failed" for s in self.last_event.get("searches", [])):
            self.console.print(
                "Some web requests failed; current information may be unverified.",
                style="yellow",
            )
        if error:
            self.console.print(Text(terminal_text(error), style="red"))
        if status != "complete":
            self.console.print(
                "This partial reply is excluded from future context.", style="dim"
            )
        self.console.print()

    async def run(self) -> None:
        bindings = KeyBindings()

        @bindings.add("enter")
        def submit(event):
            event.current_buffer.validate_and_handle()

        @bindings.add("escape", "enter")
        def newline(event):
            event.current_buffer.insert_text("\n")

        @bindings.add("c-t")
        def trace(event):
            run_in_terminal(self.show_trace)

        prompt = PromptSession(
            input=self.input,
            history=InMemoryHistory(),
            multiline=True,
            key_bindings=bindings,
            prompt_continuation="  ",
            style=Style.from_dict({"prompt": f"bold {ACCENT}"}),
        )
        self.console.print()
        self.console.print(Text.assemble(("v. ", f"bold {ACCENT}"), ("Vikram", "bold")))
        self.console.print(
            Text(terminal_text(format_model_name(self.session.model)), style="dim")
        )
        self.console.print(
            "Local chat · supported models can use web tools automatically", style="dim"
        )
        self.help()
        while True:
            try:
                message = (await prompt.prompt_async([("class:prompt", "› ")])).strip()
            except EOFError:
                break
            except KeyboardInterrupt:
                break
            if not message:
                continue
            if message in ("/exit", "/quit"):
                break
            if message == "/help":
                self.help()
            elif message == "/trace":
                self.show_trace()
            elif message == "/activity":
                self.show_activity()
            elif message.split(maxsplit=1)[:1] == ["/web"]:
                self.console.print(
                    "There is no web setting. Supported models use web tools automatically.",
                    style="dim",
                )
            else:
                await self.answer(message)

    def help(self) -> None:
        self.console.print(
            "Enter send · Alt+Enter newline · ↑ history · Ctrl+T thinking", style="dim"
        )
        self.console.print(
            "Ctrl+C stop reply / exit input · /trace · /activity · /help · /exit\n",
            style="dim",
        )


def run_terminal(session: "ChatSession") -> None:
    with (
        contextlib.closing(create_input()) as terminal_input,
        warnings.catch_warnings(),
    ):
        # This pinned SDK migration notice belongs in developer checks, not chat.
        warnings.filterwarnings(
            "ignore",
            message=r"`httpx\.AsyncClient` support for OpenAI-compatible providers is deprecated",
            category=PydanticAIDeprecationWarning,
            module=r"vikram\.agent",
        )
        asyncio.run(TerminalChat(session, terminal_input=terminal_input).run())
