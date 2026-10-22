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
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style
from pydantic_ai import PydanticAIDeprecationWarning
from pydantic_ai.exceptions import AgentRunError
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.text import Text

from vikram.errors import ReplyError, reply_error
from vikram.interaction import (
    ActionRequest,
    AskUserRequest,
    AskUserResponse,
    PlanRequest,
    PlanReview,
    answer_from_text,
)
from vikram.model_names import format_model_name

if TYPE_CHECKING:
    from vikram.cli import ChatSession

ACCENT = "#cf884d"
STYLE = Style.from_dict({"prompt": f"bold {ACCENT}"})
ESCAPES = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
CONTROLS = dict.fromkeys([*range(0, 9), *range(11, 32), *range(127, 160)])
PHASES = {
    "thinking": "Thinking…",
    "searching": "Searching the web…",
    "fetching": "Reading sources…",
    "sandbox": "Running code in the sandbox…",
    "asking": "Waiting for your answer…",
    "reviewing": "Waiting for plan approval…",
    "browsing": "Using the browser…",
    "approving": "Waiting for your OK…",
    "paused": "Waiting for the browser…",
    "answering": "Writing…",
}
PLAN_STATUS = {
    "approved": "approved",
    "changes_requested": "changes requested",
    "not_approved": "not approved",
    "timed_out": "not reviewed in time",
    "waiting": "waiting for approval",
    "stopped": "stopped",
}


def terminal_text(value: str) -> str:
    """Keep model/tool text from supplying terminal control sequences."""
    return ESCAPES.sub("", value).translate(CONTROLS)


def activity_summary(activity: list[dict]) -> list[str]:
    """One short line per kind of tool activity in a reply."""
    lines = []
    runs = [a for a in activity if a["kind"] == "code"]
    if runs:
        failed = sum(a["status"] == "failed" for a in runs)
        lines.append(
            f"Sandbox · {len(runs)} {'run' if len(runs) == 1 else 'runs'}"
            + (f" · {failed} failed" if failed else "")
        )
    questions = [a for a in activity if a["kind"] == "question"]
    if questions:
        answered = sum(a["status"] == "answered" for a in questions)
        lines.append(
            f"Questions · {answered} of {len(questions)} answered"
            if len(questions) > 1
            else f"Question · {questions[0]['status'].replace('_', ' ')}"
        )
    plans = [a for a in activity if a["kind"] == "plan"]
    if plans:
        lines.append(f"Plan · {PLAN_STATUS.get(plans[-1]['status'], 'submitted')}")
    actions = [a for a in activity if a["kind"] == "browser"]
    if actions:
        declined = sum(a["status"] in ("denied", "refused") for a in actions)
        failed = sum(a["status"] == "failed" for a in actions)
        lines.append(
            f"Browser · {len(actions)} {'action' if len(actions) == 1 else 'actions'}"
            + (f" · {declined} not allowed" if declined else "")
            + (f" · {failed} failed" if failed else "")
        )
    return lines


BROWSER_VERBS = {
    "open": "Opened",
    "click": "Clicked",
    "type": "Typed into",
    "scroll": "Scrolled",
    "back": "Went back",
    "look": "Looked at the page",
}


def browser_summary(record: dict) -> str:
    """What one browser action did, for `/activity`."""
    text = f"Browser · {record['status'].replace('_', ' ')} · "
    text += BROWSER_VERBS.get(record["action"], record["action"])
    if record.get("target"):
        text += f" {record['target']}"
    if record.get("text"):
        text += f": “{record['text']}”"
    if record.get("title") or record.get("url"):
        text += f" → {record.get('title') or ''} {record.get('url') or ''}".rstrip()
    return text


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
    for line in activity_summary(event.get("activity", [])):
        items.append(Text(f"› {line}", style="dim"))
    answer = terminal_text(event.get("text", ""))
    if answer:
        items.append(Markdown(answer, hyperlinks=False, code_theme="ansi_dark"))
    if status == "running":
        phase = PHASES.get(event.get("phase"), "Waiting for Ollama…")
        hint = " · Ctrl+T " + ("hide" if show_thinking else "view") if reasoning else ""
        items.append(
            Text(f"● {phase} {elapsed:.0f}s{hint} · Ctrl+C stop", style=ACCENT)
        )
    else:
        items.append(Text(f"{status.capitalize()} · {elapsed:.1f}s", style="dim"))
    return Group(*items)


class TerminalInteraction:
    """Asks questions and reviews plans with ordinary terminal prompts.

    Ctrl+C raises `KeyboardInterrupt` (stop the reply); Ctrl+D or an empty answer
    declines a question or leaves a plan unapproved.
    """

    def __init__(
        self,
        console: Console,
        *,
        terminal_input: Input | None = None,
        output: Output | None = None,
    ):
        self.console = console
        self.input, self.output = terminal_input, output
        self.prompt: PromptSession | None = None

    async def _read(self, label: str) -> str:
        if self.prompt is None:
            self.prompt = PromptSession(input=self.input, output=self.output)
        return (
            await self.prompt.prompt_async([("class:prompt", label)], style=STYLE)
        ).strip()

    async def ask(self, request: AskUserRequest, /) -> AskUserResponse:
        answers = []
        for question in request.questions:
            body = Text(terminal_text(question.question) + "\n")
            for number, option in enumerate(question.options, 1):
                body.append(f"\n {number}. ", style=f"bold {ACCENT}")
                body.append(terminal_text(option.label))
                if option.description:
                    body.append(f" — {terminal_text(option.description)}", style="dim")
            body.append(
                "\n\n"
                + (
                    "Type numbers separated by commas"
                    if question.multi_select
                    else "Type a number"
                )
                + ", or your own answer. Enter skips.",
                style="dim",
            )
            self.console.print(
                Panel(
                    body,
                    title=f"Vikram asks · {terminal_text(question.header)}",
                    title_align="left",
                    border_style=ACCENT,
                )
            )
            while True:
                try:
                    text = await self._read("answer › ")
                except EOFError:
                    text = ""
                if not text:
                    self.console.print("Question skipped.", style="dim")
                    return AskUserResponse(cancelled=True)
                answer = answer_from_text(question, text)
                if answer is not None:
                    answers.append(answer)
                    break
                self.console.print(
                    "Choose listed numbers, or type an answer in words.", style="dim"
                )
        return AskUserResponse(answers=tuple(answers))

    async def review_plan(self, request: PlanRequest, /) -> PlanReview:
        self.console.print(
            Panel(
                Markdown(terminal_text(request.plan), hyperlinks=False),
                title="Plan · approve before Vikram starts",
                title_align="left",
                border_style=ACCENT,
            )
        )
        while True:
            try:
                choice = (
                    await self._read("approve? y yes · c change · n not now › ")
                ).lower()
            except EOFError:
                choice = "n"
            if choice in ("y", "yes", "a", "approve"):
                return PlanReview(decision="approve")
            if choice in ("", "n", "no", "not now"):
                return PlanReview(decision="dismiss")
            if choice in ("c", "change", "changes"):
                try:
                    feedback = await self._read("what should change? › ")
                except EOFError:
                    feedback = ""
                if feedback:
                    return PlanReview(decision="revise", feedback=feedback[:2000])
            self.console.print("Type y, c, or n.", style="dim")

    async def approve(self, request: ActionRequest, /) -> bool:
        body = Text(terminal_text(request.summary) + "\n", style="bold")
        body.append(terminal_text(request.reason), style="dim")
        if request.url:
            body.append("\n" + terminal_text(request.url), style="dim")
        self.console.print(
            Panel(
                body,
                title="Browser · allow this action?",
                title_align="left",
                border_style=ACCENT,
            )
        )
        while True:
            try:
                choice = (await self._read("allow? y yes · n no › ")).lower()
            except EOFError:
                choice = "n"
            if choice in ("y", "yes", "allow"):
                return True
            if choice in ("", "n", "no"):
                return False
            self.console.print("Type y or n.", style="dim")


class _QueuedInteraction:
    """Hands each question or plan to the terminal loop, which owns the screen."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()

    async def _submit(self, kind: str, payload):
        future = asyncio.get_running_loop().create_future()
        self.queue.put_nowait((kind, payload, future))
        return await future

    async def ask(self, request: AskUserRequest, /) -> AskUserResponse:
        return await self._submit("ask", request)

    async def review_plan(self, request: PlanRequest, /) -> PlanReview:
        return await self._submit("plan", request)

    async def approve(self, request: ActionRequest, /) -> bool:
        return await self._submit("approve", request)


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
        terminal_output: Output | None = None,
    ):
        self.session = session
        self.console = console or Console()
        self.input, self.output = terminal_input, terminal_output
        self.last_event: dict = {}
        self.show_thinking = False
        self.prompter = TerminalInteraction(
            self.console, terminal_input=terminal_input, output=terminal_output
        )

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
        activity = self.last_event.get("activity", [])
        if not searches and not activity:
            self.console.print("No tool activity in the latest reply.", style="dim")
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
        for item in activity:
            if item["kind"] == "code":
                self.console.print(
                    Text(f"Sandbox · {item['status']}", style="dim"), Rule(style="dim")
                )
                self.console.print(
                    Syntax(terminal_text(item["code"]), "python", theme="ansi_dark")
                )
                result = item.get("error") or item.get("output") or "(no output)"
                self.console.print(Text(terminal_text(result), style="dim"))
            elif item["kind"] == "question":
                answers = item.get("answers") or {}
                for question in item["questions"]:
                    picked = answers.get(question["header"])
                    self.console.print(
                        Text(
                            terminal_text(
                                f"Question · {question['question']} → "
                                + (
                                    ", ".join(picked)
                                    if picked
                                    else item["status"].replace("_", " ")
                                )
                            ),
                            style="dim",
                        )
                    )
            elif item["kind"] == "browser":
                self.console.print(
                    Text(terminal_text(browser_summary(item)), style="dim")
                )
                if item.get("error"):
                    self.console.print(Text(terminal_text(item["error"]), style="dim"))
            else:
                self.console.print(
                    Text(
                        f"Plan · {PLAN_STATUS.get(item['status'], item['status'])}",
                        style="dim",
                    )
                )
                self.console.print(Markdown(terminal_text(item["plan"])))
                if item.get("feedback"):
                    self.console.print(
                        Text(
                            terminal_text(f"Feedback: {item['feedback']}"), style="dim"
                        )
                    )

    async def _interact(self, kind: str, payload, future, task) -> None:
        """Answer one queued question or plan outside the live display."""

        async def ask():
            # Inside a task, an escaping KeyboardInterrupt would stop the event loop.
            try:
                if kind == "ask":
                    return await self.prompter.ask(payload)
                if kind == "approve":
                    return await self.prompter.approve(payload)
                return await self.prompter.review_plan(payload)
            except KeyboardInterrupt:
                return None

        prompt = asyncio.ensure_future(ask())
        # The reply may stop waiting (a stop or the shared time limit): close the prompt.
        future.add_done_callback(lambda done: prompt.cancel())
        try:
            result = await prompt
        except asyncio.CancelledError:
            if future.done() and not asyncio.current_task().cancelling():
                return
            raise
        if result is None:
            # Ctrl+C at a prompt stops the whole reply, as it does while streaming.
            future.cancel()
            task.cancel()
        elif not future.done():
            future.set_result(result)

    async def answer(self, message: str) -> None:
        self.last_event = {"phase": "loading"}
        self.show_thinking = False
        started = time.monotonic()
        status, error, notices = "complete", None, []
        stopped = False
        planning = self.session.mode == "plan"
        self.console.print()
        self.console.print(
            Text.assemble(
                ("v. ", f"bold {ACCENT}"),
                ("Vikram", "bold"),
                (f" · {terminal_text(format_model_name(self.session.model))}", "dim"),
                (" · plan mode" if planning else "", ACCENT),
            )
        )
        interaction = _QueuedInteraction()

        async def consume():
            async with contextlib.aclosing(
                self.session.reply(message, interaction=interaction)
            ) as events:
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
            while not task.done():
                waiter = asyncio.ensure_future(interaction.queue.get())
                try:
                    with (
                        Live(
                            _LiveReply(self, started),
                            console=self.console,
                            refresh_per_second=8,
                            transient=True,
                        ),
                        (
                            self.input.raw_mode()
                            if self.input
                            else contextlib.nullcontext()
                        ),
                        (
                            self.input.attach(keys_ready)
                            if self.input
                            else contextlib.nullcontext()
                        ),
                    ):
                        await asyncio.wait(
                            {task, waiter}, return_when=asyncio.FIRST_COMPLETED
                        )
                finally:
                    if not waiter.done():
                        waiter.cancel()
                if waiter.done() and not waiter.cancelled():
                    await self._interact(*waiter.result(), task)
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
        elif self.last_event.get("activity"):
            self.console.print(
                "/activity shows code, answers, and browser actions", style="dim"
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
        if planning and self.session.mode == "normal":
            self.console.print("Plan approved · plan mode is off.", style=ACCENT)
        self.console.print()

    def toggle_plan(self) -> None:
        self.session.mode = "normal" if self.session.mode == "plan" else "plan"
        self.console.print(
            (
                (
                    "Plan mode on · Vikram proposes a plan for approval before it "
                    "runs code or does the work. /plan turns it off."
                )
                if self.session.mode == "plan"
                else "Plan mode off."
            ),
            style=ACCENT if self.session.mode == "plan" else "dim",
        )

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
            output=self.output,
            history=InMemoryHistory(),
            multiline=True,
            key_bindings=bindings,
            prompt_continuation="  ",
            style=STYLE,
        )
        self.console.print()
        self.console.print(Text.assemble(("v. ", f"bold {ACCENT}"), ("Vikram", "bold")))
        self.console.print(
            Text(terminal_text(format_model_name(self.session.model)), style="dim")
        )
        self.console.print(
            "Local chat · supported models can use web tools, a Python sandbox, "
            "and questions automatically",
            style="dim",
        )
        if self.session.browser is not None:
            self.console.print(
                "Browser · hidden, private, and closed when you exit; Vikram asks "
                "before it submits, signs in, buys, or posts",
                style="dim",
            )
        self.help()
        while True:
            label = "plan › " if self.session.mode == "plan" else "› "
            try:
                message = (await prompt.prompt_async([("class:prompt", label)])).strip()
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
            elif message == "/plan":
                self.toggle_plan()
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
            "Ctrl+C stop reply / exit input · /plan · /trace · /activity · /help · /exit\n",
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
        asyncio.run(_run(TerminalChat(session, terminal_input=terminal_input)))


async def _run(chat: TerminalChat) -> None:
    try:
        await chat.run()
    finally:
        # The hidden browser's private data ends with the chat.
        await chat.session.close()
