"""Terminal adapter for the shared local model/tool conversation runner."""

import argparse
import asyncio
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
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
from vikram.session import Session as ChatSession


def _write(stream, text):
    from vikram.terminal import terminal_text

    stream.write(terminal_text(text))


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
            _write(sys.stderr, event["notice"] + "\n")
            continue
        last = event
        for index, search in enumerate(event.get("searches", [])):
            marker = (index, search["status"])
            if marker in seen:
                continue
            seen.add(marker)
            if search["status"] == "running":
                arguments = search["arguments"]
                _write(
                    sys.stderr,
                    ("Searching: " if search["tool"] == "web_search" else "Reading: ")
                    + ", ".join(
                        arguments.get("search_queries", arguments.get("urls", []))
                    )
                    + "\n",
                )
            elif search.get("error"):
                _write(sys.stderr, search["error"] + "\n")
        for record in event.get("activity", []):
            marker = (record["kind"], record["id"], record["status"])
            if marker in seen:
                continue
            seen.add(marker)
            if record["kind"] == "code" and record["status"] == "running":
                _write(sys.stderr, "Running code in the sandbox…\n")
            elif record["kind"] == "code" and record["status"] == "failed":
                _write(
                    sys.stderr,
                    "Sandbox error: "
                    + ((record.get("error") or "").strip().splitlines() or [""])[-1]
                    + "\n",
                )
            elif record["kind"] == "question" and interaction is None:
                if record["status"] == "declined":
                    for question in record["questions"]:
                        _write(
                            sys.stderr,
                            f"Question skipped (no interactive terminal): "
                            f"{question['question']}\n",
                        )
            elif record["kind"] == "browser":
                line = browser_line(record)
                if line:
                    _write(sys.stderr, line + "\n")
            elif record["kind"] == "schedule":
                from vikram.terminal import task_line

                line = task_line(record)
                if line:
                    _write(sys.stderr, line + "\n")
    if last:
        _write(sys.stdout, last["text"] + "\n")
        if last.get("sources"):
            _write(sys.stdout, "\nRetrieved sources:\n")
            for source in last["sources"]:
                _write(
                    sys.stdout,
                    f"[{source['id']}] {source['title']} — {source['url']}\n",
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
    """A session with the hidden browser when this Mac has the Vikram app, and
    the scheduled tasks the app shows."""
    engine = browsers.HelperEngine() if browsers.unavailable_reason() is None else None
    return ChatSession(
        model, thinking=thinking, mode=mode, browser=engine, tasks=_task_book()
    )


def _task_book():
    """Scheduled tasks for the model; the file opens only when a tool needs it."""
    from vikram import schedules

    book = schedules.TaskBook(
        origin="cli", opener=lambda: schedules.ScheduleStore(schedules.data_root())
    )
    book.changed = lambda: schedules.ensure_agent(
        book.store, schedules.installed_agent()
    )
    return book


def _chat(
    model: str, prompt: str | None, *, thinking="on", mode="normal", options=None
) -> None:
    from vikram.settings import Settings, application_root

    options = options or argparse.Namespace()
    root = Path(
        getattr(options, "session_dir", None)
        or os.environ.get("VIKRAM_CHAT_DATA_DIR")
        or application_root()
    ).expanduser()
    settings = Settings(root=root, workspace=Path.cwd())
    if getattr(options, "list_models", False):

        async def discover():
            return await ModelCatalog().refresh(
                include_cloud=model.startswith("openrouter:")
            )

        for item in asyncio.run(discover()):
            _write(
                sys.stdout,
                f"{item['name']}\t{'available' if item['available'] else item.get('reason', 'unavailable')}\n",
            )
        return
    if getattr(options, "mode", "text") == "rpc":
        from vikram.desktop.__main__ import serve

        asyncio.run(serve(root, browser=False))
        return
    session = _session(model, thinking=thinking, mode=mode)
    store = None
    terminal_interactive = (
        prompt is None
        and sys.stdin.isatty()
        and sys.stdout.isatty()
        and os.environ.get("TERM") != "dumb"
    )
    interface = getattr(options, "tui_mode", None) or settings.get("interface")
    interactive = terminal_interactive and interface != "plain"
    if not getattr(options, "no_session", False) and (
        terminal_interactive
        or prompt is not None
        or getattr(options, "session", None)
        or getattr(options, "resume", False)
        or getattr(options, "continue_session", False)
    ):
        from vikram.desktop.store import Store

        store = Store(root)
        cid = getattr(options, "session", None)
        if getattr(options, "continue_session", False):
            chats = store.list_chats(workspace=Path.cwd())
            cid = chats[0]["id"] if chats else None
        old = session
        session = ChatSession(
            model,
            thinking=thinking,
            mode=mode,
            browser=old.browser,
            tasks=old.tasks,
            store=store,
            chat_id=cid,
            workspace=Path.cwd(),
        )
        if cid and getattr(options, "explicit_model", False):
            session.model = model
        if cid and getattr(options, "explicit_thinking", False):
            session.thinking = thinking
        if cid and getattr(options, "plan", False):
            session.mode = "plan"
        if getattr(options, "name", None):
            store.update_chat(session.chat_id, title=options.name)
    from vikram.coding import CodingSession
    from vikram.resources import ProjectResources

    settings.workspace = Path(session.workspace or Path.cwd())
    session.coding = CodingSession(settings.workspace, settings=settings)
    session.instructions = ProjectResources(settings).instructions()
    if interactive:
        from vikram.tui import TerminalApp

        async def tui():
            for path in getattr(options, "files", []):
                await session.attach_file(path)
            app = TerminalApp(
                session,
                settings=settings,
                full_screen=(
                    getattr(options, "tui_mode", None) or settings.get("interface")
                )
                != "regular",
            )
            if getattr(options, "resume", False):

                async def resume():
                    await app.ready.wait()
                    await app.command("resume", "")

                waiter = asyncio.create_task(resume())
            try:
                await app.run()
            finally:
                if getattr(options, "resume", False):
                    waiter.cancel()

        try:
            asyncio.run(tui())
        finally:
            if store:
                store.close()
        return
    if prompt is None:
        _write(
            sys.stderr,
            f"Local model · {format_model_name(model)}\n"
            "Supported models can search through Parallel automatically when needed.\n"
            "/plan toggles plan mode; /exit ends the chat.\n",
        )
    with asyncio.Runner() as runner:
        try:
            for path in getattr(options, "files", []):
                runner.run(session.attach_file(path))
            if getattr(options, "mode", "text") == "json":
                from vikram.automation import json_reply

                runner.run(json_reply(session, prompt or ""))
            else:
                _loop(runner, session, prompt)
        finally:
            # The hidden browser's private data ends with the session.
            runner.run(session.close())
            if store:
                store.close()


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
                _write(
                    sys.stderr,
                    f"Plan mode {'on' if session.mode == 'plan' else 'off'}.\n",
                )
                continue
            if message.split(maxsplit=1)[:1] == ["/web"]:
                _write(
                    sys.stderr,
                    "There is no web setting. Web tools are available automatically "
                    "for supported models.\n",
                )
                continue
            if not message:
                continue
        runner.run(_reply(session, message))
        if prompt is not None:
            return


def schedules_main(argv: list[str]) -> int:
    """List scheduled tasks; `--run-due` runs due ones now, in this terminal."""
    from vikram import schedules
    from vikram.toolkit import task_list

    parser = argparse.ArgumentParser(
        prog="vikram schedules",
        description="List the scheduled tasks that Vikram runs in the background. Ask Vikram in a chat to create, change, pause, or delete one, or use the app.",
    )
    parser.add_argument(
        "--run-due",
        action="store_true",
        help="run tasks that are due (or asked to run now) in the foreground, then exit",
    )
    args = parser.parse_args(argv)
    root = schedules.data_root()
    store = schedules.ScheduleStore(root)
    try:
        if args.run_due:
            with schedules.runner_lock(root) as lock:
                if lock is None:
                    _write(sys.stderr, "Another scheduled run is in progress.\n")
                    return 1
                count = asyncio.run(schedules.run_due(store))
            print(f"Ran {count} scheduled {'task' if count == 1 else 'tasks'}.")
            return 0
        print(task_list(store.tasks()))
        agent = schedules.installed_agent(root)
        if agent is None:
            print(
                "\nBackground runs need the installed Vikram app; use --run-due here."
            )
        else:
            loaded = schedules.agent_loaded()
            print(
                "\nBackground runs: "
                + (
                    "on"
                    if loaded
                    else (
                        "off (no enabled tasks)"
                        if not store.active()
                        else "not running"
                    )
                )
            )
        return 0
    finally:
        store.close()


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["update"]:
        from vikram.releases import update_main

        return update_main(argv[1:])
    if argv[:1] == ["schedules"]:
        return schedules_main(argv[1:])
    parser = argparse.ArgumentParser(
        description="Chat with local Ollama using the same model/tools as the app. Supported models automatically use Parallel web search, Python in an isolated VM, and clarifying questions when needed; /plan toggles plan mode and /exit ends interactive chat.",
        epilog="Use `vikram update` to update the CLI and Mac app, or `vikram update --check` to check for a release. `vikram schedules` lists scheduled tasks. Use `vikram -- update` to send 'update' as a prompt.",
    )
    parser.add_argument(
        "--version", action="version", version=f"Vikram {cli_version()}"
    )
    parser.add_argument(
        "prompt",
        nargs="*",
        help="prompt text and optional @file paths; omit for interactive chat",
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
    parser.add_argument("--tui-mode", choices=("fullscreen", "regular", "plain"))
    parser.add_argument("--mode", choices=("text", "json", "rpc"), default="text")
    parser.add_argument("--print", "-p", dest="print_once", action="store_true")
    parser.add_argument("--list-models", action="store_true")
    parser.add_argument(
        "--continue", "-c", dest="continue_session", action="store_true"
    )
    parser.add_argument("--resume", "-r", action="store_true")
    parser.add_argument("--session", metavar="ID")
    parser.add_argument("--session-dir", metavar="DIRECTORY")
    parser.add_argument("--no-session", action="store_true")
    parser.add_argument("--name")
    args = parser.parse_args(argv)
    args.files = [
        value[1:] for value in args.prompt if value.startswith("@") and len(value) > 1
    ]
    args.prompt = (
        " ".join(value for value in args.prompt if not value.startswith("@")) or None
    )
    if args.files and args.prompt is None:
        args.prompt = "Attached files."
    args.explicit_model = any(
        value == "--model" or value.startswith("--model=") for value in argv
    )
    args.explicit_thinking = any(
        value == "--thinking" or value.startswith("--thinking=") for value in argv
    )
    if not args.explicit_thinking:
        from vikram.settings import Settings

        preference_root = args.session_dir or os.environ.get("VIKRAM_CHAT_DATA_DIR")
        args.thinking = Settings(
            root=Path(preference_root) if preference_root else None,
            workspace=Path.cwd(),
        ).get("thinking", "on")
    if not args.explicit_model and not os.environ.get("OLLAMA_MODEL"):
        from vikram.settings import Settings

        saved_root = args.session_dir or os.environ.get("VIKRAM_CHAT_DATA_DIR")
        saved_model = Settings(
            root=Path(saved_root) if saved_root else None, workspace=Path.cwd()
        ).get("model")
        if saved_model:
            args.model = saved_model
    if args.mode != "rpc" and not sys.stdin.isatty():
        import stat

        try:
            piped = stat.S_ISFIFO(os.fstat(sys.stdin.fileno()).st_mode)
        except (OSError, ValueError):
            piped = False
        if piped:
            content = sys.stdin.read(1_000_001)
            if len(content) > 1_000_000:
                parser.error("piped input exceeds 1,000,000 characters")
            if content.strip():
                args.prompt = content + ("\n\n" + args.prompt if args.prompt else "")
    if args.print_once and args.prompt is None:
        parser.error("--print needs a prompt or piped input")
    if args.no_session and (args.session or args.resume or args.continue_session):
        parser.error("--no-session cannot resume a saved session")
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
            options=args,
        )
    except KeyboardInterrupt:
        _write(sys.stderr, "\n")
        return 130
    except ValueError as exc:
        _write(sys.stderr, str(exc) + "\n")
        return 1
    except (ReplyError, AgentRunError) as exc:
        _write(sys.stderr, reply_error(exc) + "\n")
        return 1
    return 0
