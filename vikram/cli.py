"""Terminal adapter for the shared local model/tool conversation runner."""

import argparse
import asyncio
import os
import signal
import sys
from collections.abc import Sequence
from uuid import uuid4

from pydantic_ai.exceptions import AgentRunError

from vikram import browser as browsers
from vikram.agent import DEFAULT_MODEL
from vikram.context import context_messages
from vikram.conversation import stream_reply
from vikram.errors import ReplyError, reply_error
from vikram.installation import cli_version
from vikram.model_names import format_model_name
from vikram.models import ModelCatalog, prepare_model, thinking_selection


class ChatSession:
    """One terminal conversation. `browser` is its hidden browser engine, which
    keeps its pages across replies until `close()`."""

    def __init__(
        self,
        model: str,
        *,
        thinking="on",
        streamer=stream_reply,
        mode="normal",
        browser=None,
    ):
        self.model, self.thinking, self.streamer = model, thinking, streamer
        self.mode = mode
        self.browser = browser
        self.turns: list[dict] = []
        self.session_id = uuid4().hex
        self.catalog = ModelCatalog()

    async def close(self) -> None:
        if self.browser is not None and hasattr(self.browser, "close"):
            await self.browser.close()

    async def reply(self, prompt: str, *, interaction=None):
        models = await self.catalog.refresh()
        selected, setting, thinking, web, notice = prepare_model(
            models, self.model, thinking_selection(self.thinking)
        )
        if setting is None:
            self.thinking = "on"
        if notice:
            yield {"notice": notice}
        turn = {
            "prompt": [prompt],
            "answer": None,
            "sources": [],
            "model": None,
            "activity": [],
        }
        iterator = self.streamer(
            self.model,
            thinking,
            context_messages(self.turns),
            [prompt],
            web=web,
            session_id=self.session_id,
            mode=self.mode,
            interaction=interaction,
            browser=self.browser,
            vision=bool(selected.get("vision")),
        )
        last = None
        try:
            async for event in iterator:
                last = event
                yield event
            if last and last["text"]:
                turn.update(
                    answer=last["text"],
                    sources=last.get("sources", []),
                    model=last.get("actual_model"),
                    activity=last.get("activity", []),
                )
        finally:
            await iterator.aclose()
            self.turns.append(turn)
            # An approved plan ends plan mode, as the desktop app does.
            if last and last.get("mode") == "normal":
                self.mode = "normal"


class _PromptInteraction:
    """Ask in the terminal for one-shot or plain chats; Ctrl+C quits as usual."""

    def __init__(self):
        from prompt_toolkit.output import create_output
        from rich.console import Console

        from vikram.terminal import TerminalInteraction

        self.prompter = TerminalInteraction(
            Console(stderr=True), output=create_output(stdout=sys.stderr)
        )

    async def _guard(self, waiting):
        try:
            return await waiting
        except KeyboardInterrupt:
            # Inside the reply task: route through asyncio's normal Ctrl+C handling.
            signal.raise_signal(signal.SIGINT)
            raise asyncio.CancelledError from None

    async def ask(self, request, /):
        return await self._guard(self.prompter.ask(request))

    async def review_plan(self, request, /):
        return await self._guard(self.prompter.review_plan(request))

    async def approve(self, request, /):
        return await self._guard(self.prompter.approve(request))


def _interaction():
    if sys.stdin.isatty() and sys.stderr.isatty():
        return _PromptInteraction()
    return None


async def _reply(session: ChatSession, message: str) -> None:
    last, seen = None, set()
    interaction = _interaction()
    async for event in session.reply(message, interaction=interaction):
        if event.get("notice"):
            sys.stderr.write(event["notice"] + "\n")
            continue
        last = event
        for index, search in enumerate(event.get("searches", [])):
            marker = (index, search["status"])
            if marker in seen:
                continue
            seen.add(marker)
            if search["status"] == "running":
                arguments = search["arguments"]
                sys.stderr.write(
                    ("Searching: " if search["tool"] == "web_search" else "Reading: ")
                    + ", ".join(
                        arguments.get("search_queries", arguments.get("urls", []))
                    )
                    + "\n"
                )
            elif search.get("error"):
                sys.stderr.write(search["error"] + "\n")
        for record in event.get("activity", []):
            marker = (record["kind"], record["id"], record["status"])
            if marker in seen:
                continue
            seen.add(marker)
            if record["kind"] == "code" and record["status"] == "running":
                sys.stderr.write("Running code in the sandbox…\n")
            elif record["kind"] == "code" and record["status"] == "failed":
                sys.stderr.write(
                    "Sandbox error: "
                    + ((record.get("error") or "").strip().splitlines() or [""])[-1]
                    + "\n"
                )
            elif record["kind"] == "question" and interaction is None:
                if record["status"] == "declined":
                    for question in record["questions"]:
                        sys.stderr.write(
                            f"Question skipped (no interactive terminal): "
                            f"{question['question']}\n"
                        )
            elif record["kind"] == "browser":
                line = browser_line(record)
                if line:
                    sys.stderr.write(line + "\n")
    if last:
        sys.stdout.write(last["text"] + "\n")
        if last.get("sources"):
            sys.stdout.write("\nRetrieved sources:\n")
            for source in last["sources"]:
                sys.stdout.write(
                    f"[{source['id']}] {source['title']} — {source['url']}\n"
                )


def browser_line(record: dict) -> str | None:
    """One terminal line per browser action as it starts, waits, or ends."""
    target = record.get("target") or ""
    status = record["status"]
    if status == "running":
        verb = {
            "open": "Opening",
            "click": "Clicking",
            "type": "Typing into",
            "scroll": "Scrolling",
            "back": "Going back",
            "look": "Looking at the page",
        }.get(record["action"], "Browsing")
        return f"Browser: {verb} {target}".rstrip()
    if status == "paused":
        return None
    if status in ("failed", "refused", "timed_out") and record.get("error"):
        return "Browser: " + record["error"].splitlines()[0]
    if status == "denied":
        summary = (record.get("approval") or {}).get("summary") or "the action"
        return f"Browser: not allowed: {summary}"
    return None


def _session(model: str, *, thinking="on", mode="normal") -> ChatSession:
    """A session with the hidden browser when this Mac has the Vikram app."""
    engine = browsers.HelperEngine() if browsers.unavailable_reason() is None else None
    return ChatSession(model, thinking=thinking, mode=mode, browser=engine)


def _chat(model: str, prompt: str | None, *, thinking="on", mode="normal") -> None:
    session = _session(model, thinking=thinking, mode=mode)
    if (
        prompt is None
        and sys.stdin.isatty()
        and sys.stdout.isatty()
        and os.environ.get("TERM") != "dumb"
    ):
        from vikram.terminal import run_terminal

        run_terminal(session)
        return
    if prompt is None:
        sys.stderr.write(
            f"Local model · {format_model_name(model)}\n"
            "Supported models can search through Parallel automatically when needed.\n"
            "/plan toggles plan mode; /exit ends the chat.\n"
        )
    with asyncio.Runner() as runner:
        try:
            _loop(runner, session, prompt)
        finally:
            # The hidden browser's private data ends with the session.
            runner.run(session.close())


def _loop(runner: asyncio.Runner, session: ChatSession, prompt: str | None) -> None:
    while True:
        message = prompt
        if message is None:
            try:
                message = input("You: ").strip()
            except EOFError:
                return
            if message == "/exit":
                return
            if message == "/plan":
                session.mode = "normal" if session.mode == "plan" else "plan"
                sys.stderr.write(
                    f"Plan mode {'on' if session.mode == 'plan' else 'off'}.\n"
                )
                continue
            if message.split(maxsplit=1)[:1] == ["/web"]:
                sys.stderr.write(
                    "There is no web setting. Web tools are available automatically "
                    "for supported models.\n"
                )
                continue
            if not message:
                continue
        runner.run(_reply(session, message))
        if prompt is not None:
            return


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["update"]:
        from vikram.releases import update_main

        return update_main(argv[1:])
    parser = argparse.ArgumentParser(
        description="Chat with local Ollama using the same model/tools as the app. Supported models automatically use Parallel web search, Python in an isolated VM, and clarifying questions when needed; /plan toggles plan mode and /exit ends interactive chat.",
        epilog="Use `vikram update` to update the CLI and Mac app, or `vikram update --check` to check for a release. Use `vikram -- update` to send 'update' as a prompt.",
    )
    parser.add_argument(
        "--version", action="version", version=f"Vikram {cli_version()}"
    )
    parser.add_argument(
        "prompt", nargs="?", help="one prompt; omit for interactive chat"
    )
    default_model = os.environ.get("OLLAMA_MODEL", "").strip() or DEFAULT_MODEL
    parser.add_argument(
        "--model",
        default=default_model,
        metavar="MODEL_ID",
        help=f"local Ollama model ID (default: {format_model_name(default_model)}; ID: %(default)s; honors OLLAMA_MODEL)",
    )
    parser.add_argument(
        "--thinking",
        default="on",
        help="on (default) or off, or a named level advertised by the model",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="start in plan mode: Vikram proposes a plan for your approval before it runs code or does the work",
    )
    args = parser.parse_args(argv)
    if args.prompt is not None and not args.prompt.strip():
        parser.error("prompt must not be empty")
    args.model = args.model.strip()
    if not args.model:
        parser.error("model must not be empty")
    try:
        _chat(
            args.model,
            args.prompt,
            thinking=args.thinking,
            mode="plan" if args.plan else "normal",
        )
    except KeyboardInterrupt:
        sys.stderr.write("\n")
        return 130
    except ValueError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 1
    except (ReplyError, AgentRunError) as exc:
        sys.stderr.write(reply_error(exc) + "\n")
        return 1
    return 0
