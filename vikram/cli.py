from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vikram.streaming import tool_result_from_event as _tool_result_from_event
from vikram.streaming import tool_results_from_event as _tool_results_from_event
from vikram.streaming import tool_use_from_event as _tool_use_from_event

if TYPE_CHECKING:
    from rich.console import Console

    from vikram.agent import VikramAgent
    from vikram.settings import VikramSettings

CODE_THEME = "monokai"
HISTORY_PATH = Path.home() / ".vikram" / "cli_history"


class _CommandAutoSuggest:
    """Auto-suggest slash commands, falling back to history.

    Replaces the dependency on ``pydantic_ai._cli.CustomAutoSuggest`` so the
    interactive prompt can still complete ``/help``, ``/clear`` and friends.
    """

    def __init__(self, commands: list[str]) -> None:
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory

        self._commands = commands
        self._history = AutoSuggestFromHistory()

    def get_suggestion(self, buffer: Any, document: Any) -> Any:
        from prompt_toolkit.auto_suggest import Suggestion

        text = document.text_before_cursor.strip()
        if text:
            for command in self._commands:
                if command.startswith(text) and command != text:
                    return Suggestion(command[len(text) :])
        return self._history.get_suggestion(buffer, document)

    async def get_suggestion_async(self, buffer: Any, document: Any) -> Any:
        return self.get_suggestion(buffer, document)


def _version_string() -> str:
    from vikram import __version__
    from vikram.update import load_metadata

    meta = load_metadata()
    sha = meta.get("git_sha")
    if sha:
        return f"vikram {__version__} @ {str(sha)[:12]}"
    return f"vikram {__version__}"


class _LazyVersionAction(argparse.Action):
    def __init__(
        self,
        option_strings: Sequence[str],
        dest: str = argparse.SUPPRESS,
        default: Any = argparse.SUPPRESS,
        help: str | None = None,
    ) -> None:
        super().__init__(
            option_strings=list(option_strings),
            dest=dest,
            default=default,
            nargs=0,
            help=help,
        )

    def __call__(self, parser, namespace, values, option_string=None):  # type: ignore[override]
        print(_version_string())
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vikram",
        epilog="Commands: vikram configure, vikram update",
    )
    parser.add_argument(
        "--version",
        action=_LazyVersionAction,
        help="Show version (with install SHA if available) and exit.",
    )
    parser.add_argument(
        "--agent",
        default=None,
        help="Agent name to load from spec/ (default: vikram)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one prompt and exit instead of starting interactive chat.",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help=(
            "Prompt text, '-' for stdin, '@path' for a prompt file, or an "
            "existing file path."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one-shot output as JSON.",
    )
    parser.add_argument(
        "-y",
        "--yes",
        "--approve-all",
        dest="approve_all",
        action="store_true",
        help=(
            "Auto-approve every tool call without prompting. Useful for "
            "unattended --once runs."
        ),
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help=(
            "Hide thinking and tool-call events in interactive chat; only "
            "stream the final reply."
        ),
    )
    return parser


def read_prompt(value: str) -> str:
    if value == "-":
        return sys.stdin.read()

    if value.startswith("@") and len(value) > 1:
        return Path(value[1:]).expanduser().read_text(encoding="utf-8")

    path = Path(value).expanduser()
    if path.is_file():
        return path.read_text(encoding="utf-8")

    return value


def main(argv: Sequence[str] | None = None) -> None:
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    if raw_args and raw_args[0] == "update":
        from vikram.update import run as run_update

        sys.exit(run_update(raw_args[1:]))
    if raw_args and raw_args[0] == "configure":
        from vikram.config import run_configure

        code = run_configure(raw_args[1:])
        if code:
            sys.exit(code)
        return

    parser = build_parser()
    args = parser.parse_args(raw_args)
    if args.prompt is not None and not args.once:
        parser.error("--prompt requires --once")
    if args.json and not args.once:
        parser.error("--json requires --once")
    if args.once and args.prompt is None:
        parser.error("--once requires --prompt")
    if args.quiet and args.once:
        parser.error("--quiet cannot be combined with --once")

    from vikram.agent import build_agent
    from vikram.settings import VikramSettings
    from vikram.spec import load_spec

    settings = VikramSettings()
    if args.agent:
        settings = settings.model_copy(update={"default_agent": args.agent})
    spec = load_spec(settings.default_agent, settings.spec_root)
    agent = build_agent(spec=spec, settings=settings, approve_all=args.approve_all)

    if args.once:
        result = agent.run_sync(read_prompt(args.prompt))
        output = str(result.output)
        if args.json:
            print(json.dumps({"agent": spec.name, "output": output}))
        else:
            print(output)
        return

    import asyncio

    asyncio.run(
        run_interactive(
            agent,
            prog_name=spec.name,
            quiet=args.quiet,
            keep_servers_warm=bool(spec.mcp_servers),
            settings=settings,
        )
    )


async def run_interactive(
    agent: "VikramAgent",
    *,
    prog_name: str,
    quiet: bool,
    keep_servers_warm: bool = False,
    settings: "VikramSettings" | None = None,
) -> None:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from rich.console import Console

    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.touch(exist_ok=True)

    session: PromptSession[Any] = PromptSession(history=FileHistory(str(HISTORY_PATH)))
    console = Console()
    messages: list[Any] = []
    multiline = False
    auto_suggest = _CommandAutoSuggest(
        ["/help", "/clear", "/markdown", "/multiline", "/exit"]
    )
    context_percent = 0
    context_warned = False

    async with contextlib.AsyncExitStack() as stack:
        # Enter the agent once when MCP servers are configured so they stay
        # connected for the whole session instead of restarting every turn.
        if keep_servers_warm and hasattr(agent, "__aenter__"):
            await stack.enter_async_context(agent)

        _print_banner(console, prog_name, settings)

        while True:
            try:
                if settings is not None and settings.context_window_tokens > 0:
                    prompt_prefix = f"{prog_name} ({context_percent}%) ➤ "
                else:
                    prompt_prefix = f"{prog_name} ➤ "
                text = await session.prompt_async(
                    prompt_prefix, auto_suggest=auto_suggest, multiline=multiline
                )
            except (KeyboardInterrupt, EOFError):
                console.print("[dim]Exiting…[/dim]")
                return

            if not text.strip():
                continue

            ident_prompt = text.lower().strip().replace(" ", "-")
            if ident_prompt.startswith("/"):
                if ident_prompt in {"/help", "/?", "/h"}:
                    _print_help(console)
                    continue
                if ident_prompt in {"/clear", "/reset"}:
                    messages = []
                    context_percent = 0
                    context_warned = False
                    console.print("[dim]Conversation history cleared.[/dim]\n")
                    continue
                should_exit, multiline = _handle_slash_command(
                    ident_prompt, messages, multiline, console
                )
                if should_exit:
                    return
                continue

            try:
                messages, percent = await _render_turn(
                    agent,
                    text,
                    messages,
                    console,
                    quiet=quiet,
                    settings=settings,
                )
                if percent is not None:
                    context_percent = percent
                    context_warned = _maybe_warn_context(
                        console, context_percent, context_warned, settings
                    )
            except KeyboardInterrupt:
                console.print("[dim]Interrupted[/dim]")
            except Exception as exc:  # pragma: no cover - surface anything to user
                console.print(f"\n[red]{type(exc).__name__}[/red]: {exc}")


def _print_banner(
    console: "Console", prog_name: str, settings: "VikramSettings" | None
) -> None:
    model = getattr(settings, "model", None) if settings is not None else None
    provider = (
        getattr(settings, "model_provider", None) if settings is not None else None
    )
    if model and provider:
        console.print(
            f"[bold cyan]{prog_name}[/bold cyan] [dim]·[/dim] "
            f"{model} [dim]({provider})[/dim]",
            highlight=False,
        )
    else:
        console.print(f"[bold cyan]{prog_name}[/bold cyan]", highlight=False)
    console.print(
        "[dim]Type [/dim][cyan]/help[/cyan][dim] for commands · "
        "[/dim][cyan]/exit[/cyan][dim] or Ctrl-D to quit[/dim]\n",
        highlight=False,
    )


def _print_help(console: "Console") -> None:
    from rich.table import Table

    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    table.add_column(style="cyan", no_wrap=True)
    table.add_column(style="dim")
    commands = [
        ("/help", "Show this help (also /? or /h)"),
        ("/clear", "Clear conversation history (also /reset)"),
        ("/multiline", "Toggle multiline input (Esc then Enter to send)"),
        ("/markdown", "Show the raw message history as Markdown"),
        ("/exit", "Quit the session (also /quit, /q, Ctrl-D)"),
    ]
    for name, description in commands:
        table.add_row(name, description)
    console.print(table)
    console.print()


def _maybe_warn_context(
    console: "Console",
    percent: int,
    already_warned: bool,
    settings: "VikramSettings" | None,
) -> bool:
    """Warn once when context usage crosses ``context_warning_ratio``.

    Returns the new warned state: ``True`` while usage stays above the
    threshold, ``False`` once it drops back below so a later crossing warns
    again (e.g. after ``/clear``).
    """
    if settings is None or settings.context_window_tokens <= 0:
        return already_warned
    warning_ratio = settings.context_warning_ratio
    if warning_ratio <= 0:
        return already_warned
    threshold = round(warning_ratio * 100)
    if percent < threshold:
        return False
    if not already_warned:
        console.print(
            f"[yellow]⚠ Context window {percent}% full — use /clear or /reset "
            "to start fresh.[/yellow]\n",
            highlight=False,
        )
    return True


async def _render_turn(
    agent: "VikramAgent",
    prompt: str,
    messages: list[Any],
    console: "Console",
    *,
    quiet: bool,
    settings: "VikramSettings" | None = None,
) -> tuple[list[Any], int | None]:
    tool_timers: dict[str, float] = {}
    result = None

    # Show a spinner while waiting for the model so the screen is not blank
    # during first-token latency. console.status is only available on a real
    # rich Console, so guard for the lightweight consoles used in tests.
    status = (
        console.status("[dim]Thinking…[/dim]", spinner="dots")
        if hasattr(console, "status")
        else None
    )
    if status is not None:
        status.start()
    status_active = status is not None

    def _stop_status() -> None:
        nonlocal status_active
        if status_active and status is not None:
            status.stop()
            status_active = False

    try:
        response_needs_newline = False
        stream = _stream_agent(agent, prompt, messages)
        async for event in stream:
            if isinstance(event, dict) and "vikram_result" in event:
                result = event["vikram_result"]
                continue
            _stop_status()
            needs_newline = await _render_stream_event(
                event,
                console,
                quiet=quiet,
                tool_timers=tool_timers,
            )
            if needs_newline is not None:
                response_needs_newline = needs_newline

        if response_needs_newline:
            console.print()

        if result is None:
            result = await agent.run(prompt, message_history=messages)
    finally:
        _stop_status()

    percent = _context_percent(result, settings)
    all_messages = getattr(result, "all_messages", None)
    if callable(all_messages):
        return list(all_messages()), percent
    return list(getattr(result, "messages", []) or []), percent


async def _stream_agent(
    agent: Any, prompt: str, messages: list[Any]
) -> AsyncIterator[Any]:
    stream_events = getattr(agent, "stream_events", None)
    if callable(stream_events):
        async for event in stream_events(prompt, message_history=messages):
            yield event
        return
    stream_async = getattr(agent, "stream_async", None)
    if callable(stream_async):
        async for event in stream_async(prompt):
            yield event
        return


async def _render_stream_event(
    event: Any,
    console: "Console",
    *,
    quiet: bool,
    tool_timers: dict[str, float],
) -> bool | None:
    import time

    if not isinstance(event, dict):
        return None

    response_needs_newline: bool | None = None

    reasoning = event.get("reasoningText")
    if reasoning and not quiet:
        console.print("[dim]· thinking:[/dim]")
        for line in str(reasoning).splitlines():
            console.print(f"  [dim]{line}[/dim]")
        response_needs_newline = False

    data = event.get("data")
    if data:
        text = str(data)
        console.print(text, end="")
        response_needs_newline = not text.endswith("\n")

    tool_use = _tool_use_from_event(event)
    if tool_use is not None:
        tool_id = str(tool_use.get("toolUseId") or tool_use.get("id") or "")
        if tool_id:
            tool_timers[tool_id] = time.monotonic()
        if not quiet:
            name = str(tool_use.get("name") or "?")
            args_repr = _format_call_args(tool_use)
            console.print(f"\n[cyan]→ {name}({args_repr})[/cyan]")
            response_needs_newline = False

    for tool_result in _tool_results_from_event(event):
        if quiet:
            continue
        tool_id = str(tool_result.get("toolUseId") or tool_result.get("id") or "")
        start = tool_timers.pop(tool_id, None)
        duration = time.monotonic() - start if start is not None else None
        duration_str = f" [dim]{duration:.1f}s[/dim]" if duration is not None else ""
        status = str(tool_result.get("status") or "success")
        marker = "[red]✗[/red]" if status == "error" else "[green]✓[/green]"
        console.print(f"{marker} tool result{duration_str}")
        body = _stringify_tool_result(tool_result)
        if body:
            for line in body.splitlines():
                console.print(f"  [dim]{line}[/dim]")
        console.print()
        response_needs_newline = False

    return response_needs_newline


def _handle_slash_command(
    command: str,
    messages: list[Any],
    multiline: bool,
    console: "Console",
) -> tuple[bool, bool]:
    from rich.markdown import Markdown

    if command in {"/exit", "/quit", "/q"}:
        return True, multiline
    if command == "/multiline":
        multiline = not multiline
        console.print(f"[dim]multiline {'on' if multiline else 'off'}[/dim]")
        return False, multiline
    if command == "/markdown":
        console.print(Markdown(json.dumps(messages, default=str, indent=2)))
        return False, multiline
    if command == "/cp":
        console.print("[dim]copy is not available in this CLI runtime[/dim]")
        return False, multiline
    console.print(f"[dim]Unknown command: {command}[/dim]")
    return False, multiline


def _context_percent(result: Any, settings: "VikramSettings" | None) -> int | None:
    if settings is None:
        return None
    context_window = settings.context_window_tokens
    usage_fn = getattr(result, "usage", None)
    if context_window <= 0 or not callable(usage_fn):
        return None
    try:
        usage = usage_fn()
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    except Exception:
        return None
    if input_tokens <= 0:
        return None
    return round((input_tokens / context_window) * 100)


def _format_call_args(part: Any) -> str:
    if isinstance(part, dict):
        args = part.get("input") or part.get("args") or {}
        if not isinstance(args, dict):
            return _truncate(str(args))
        if not args:
            return ""
        return ", ".join(
            f"{k}={_truncate(_repr_value(v), 120)}" for k, v in args.items()
        )
    try:
        args = part.args_as_dict()
    except Exception:
        return _truncate(str(getattr(part, "args", "")) or "")
    if not args:
        return ""
    return ", ".join(f"{k}={_repr_value(v)}" for k, v in args.items())


def _repr_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    try:
        return json.dumps(value, default=str)
    except Exception:
        return repr(value)


def _stringify_tool_return(part: Any) -> str:
    try:
        items = part.content_items(mode="str")
    except Exception:
        return str(part.content)
    rendered: list[str] = []
    for item in items:
        if isinstance(item, str):
            rendered.append(item)
        else:
            rendered.append(f"<{type(item).__name__}>")
    return "\n".join(rendered)


def _stringify_tool_result(result: dict[str, Any]) -> str:
    rendered: list[str] = []
    for item in result.get("content") or []:
        if isinstance(item, dict):
            if "text" in item:
                rendered.append(str(item["text"]))
            elif "json" in item:
                rendered.append(json.dumps(item["json"], default=str))
        else:
            rendered.append(str(item))
    return "\n".join(rendered)


def _stringify_retry_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, default=str, indent=2)
    except Exception:
        return str(content)


def _truncate(text: str, limit: int = 200) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


if __name__ == "__main__":
    main()
