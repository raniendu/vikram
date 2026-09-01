from __future__ import annotations

import argparse
import contextlib
import difflib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vikram.streaming import tool_results_from_event as _tool_results_from_event
from vikram.streaming import tool_use_from_event as _tool_use_from_event

if TYPE_CHECKING:
    from rich.console import Console

    from vikram.agent import VikramAgent
    from vikram.settings import VikramSettings

CODE_THEME = "monokai"
HISTORY_PATH = Path.home() / ".vikram" / "cli_history"
COMMANDS = {
    "exec": "Run one task non-interactively",
    "configure": "Configure model providers (alias: setup)",
    "doctor": "Check configuration and workspace health",
    "update": "Check for or install Vikram updates",
}


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
    command_help = "\n".join(
        f"  {name:<10} {description}" for name, description in COMMANDS.items()
    )
    parser = argparse.ArgumentParser(
        prog="vikram",
        description="Run a spec-driven coding agent in your terminal.",
        epilog=(
            f"commands:\n{command_help}\n\n"
            "examples:\n"
            "  vikram configure\n"
            "  vikram --agent coder\n"
            '  vikram exec --agent coder "summarize this repo"\n'
            "  vikram doctor"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
        "-m",
        "--model",
        default=None,
        help="Override the configured model for this run.",
    )
    parser.add_argument(
        "-C",
        "--cd",
        type=_existing_directory,
        default=None,
        metavar="PATH",
        help="Set the agent working directory before the run starts.",
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


def build_exec_parser() -> argparse.ArgumentParser:
    """Build the Codex-style non-interactive command parser."""
    parser = argparse.ArgumentParser(
        prog="vikram exec",
        description=(
            "Run Vikram non-interactively. If PROMPT is omitted, read it from stdin."
        ),
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        metavar="PROMPT",
        help="Prompt text, '-', '@path', or an existing prompt file path.",
    )
    parser.add_argument(
        "--agent",
        default=None,
        help="Agent name to load from spec/ (default: vikram)",
    )
    parser.add_argument(
        "-m",
        "--model",
        default=None,
        help="Override the configured model for this run.",
    )
    parser.add_argument(
        "-C",
        "--cd",
        type=_existing_directory,
        default=None,
        metavar="PATH",
        help="Set the agent working directory before the run starts.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the final result as one JSON object.",
    )
    parser.add_argument(
        "-o",
        "--output-last-message",
        type=Path,
        default=None,
        metavar="PATH",
        help="Also write the final agent message to a file.",
    )
    parser.add_argument(
        "-y",
        "--yes",
        "--approve-all",
        dest="approve_all",
        action="store_true",
        help="Auto-approve every tool call without prompting.",
    )
    return parser


def _existing_directory(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f"not a directory: {value}")
    return path


def read_prompt(value: str) -> str:
    if value == "-":
        return sys.stdin.read()

    if value.startswith("@") and len(value) > 1:
        return Path(value[1:]).expanduser().read_text(encoding="utf-8")

    path = Path(value).expanduser()
    if path.is_file():
        return path.read_text(encoding="utf-8")

    return value


def _log() -> Any:
    """Return this module's logger, importing ``vikram.logging`` lazily.

    ``vikram/cli.py`` is runnable as a file (``python vikram/cli.py --help``),
    which puts ``vikram/`` itself on ``sys.path`` and makes ``vikram/logging.py``
    shadow the standard library's ``logging``. Deferring the import keeps that
    entry point working, and matches how the rest of this module imports.
    """
    from vikram.logging import get_logger

    return get_logger("vikram.cli")


def _cli_log_level(settings: Any) -> str:
    """Pick the CLI's log level.

    The terminal belongs to the conversation, so info-level chatter is muted
    unless the operator explicitly asked for it with ``VIKRAM_LOG_LEVEL``.
    Warnings and errors always reach stderr.
    """
    if os.environ.get("VIKRAM_LOG_LEVEL"):
        return settings.log_level
    return "WARNING"


def main(argv: Sequence[str] | None = None) -> None:
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    if raw_args and raw_args[0] == "exec":
        parser = build_exec_parser()
        args = parser.parse_args(raw_args[1:])
        prompt = _read_exec_prompt(args.prompt, parser)
        _run(
            args,
            prompt=prompt,
            json_output=args.json,
            output_last_message=args.output_last_message,
        )
        return
    if raw_args and raw_args[0] == "update":
        from vikram.update import run as run_update

        sys.exit(run_update(raw_args[1:]))
    if raw_args and raw_args[0] in {"configure", "setup"}:
        from vikram.config import run_configure

        code = run_configure(raw_args[1:])
        if code:
            sys.exit(code)
        return
    if raw_args and raw_args[0] == "doctor":
        from vikram.doctor import run as run_doctor

        sys.exit(run_doctor(raw_args[1:]))
    if raw_args and not raw_args[0].startswith("-"):
        parser = build_parser()
        command = raw_args[0]
        suggestion = difflib.get_close_matches(command, COMMANDS, n=1)
        message = f"unknown command: {command}"
        if suggestion:
            message += f". Did you mean `vikram {suggestion[0]}`?"
        parser.error(message)

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

    if args.once:
        _run(args, prompt=read_prompt(args.prompt), json_output=args.json)
        return
    _run(args, quiet=args.quiet)


def _read_exec_prompt(value: str | None, parser: argparse.ArgumentParser) -> str:
    if value is not None:
        prompt = read_prompt(value)
        if value != "-" and not getattr(sys.stdin, "isatty", lambda: False)():
            try:
                stdin_context = sys.stdin.read()
            except OSError:
                # Test runners and embedded callers may replace stdin with an
                # object that intentionally rejects reads.
                stdin_context = ""
            if stdin_context.strip():
                prompt = f"{prompt}\n\nAdditional context from stdin:\n{stdin_context}"
    else:
        is_tty = getattr(sys.stdin, "isatty", lambda: False)()
        if is_tty:
            parser.error("PROMPT is required when stdin is a terminal")
        prompt = sys.stdin.read()
    if not prompt.strip():
        parser.error(
            "prompt is empty; pass a task as an argument or pipe content on stdin\n"
            'example: vikram exec "summarize this repo"'
        )
    return prompt


def _run(
    args: argparse.Namespace,
    *,
    prompt: str | None = None,
    quiet: bool = False,
    json_output: bool = False,
    output_last_message: Path | None = None,
) -> None:
    from vikram.agent import build_agent
    from vikram.logging import configure_logging
    from vikram.observability import init_observability
    from vikram.settings import VikramSettings
    from vikram.specstore import load_agent

    old_cwd = Path.cwd()
    try:
        if args.cd is not None:
            os.chdir(args.cd)

        settings = VikramSettings()
        overrides: dict[str, Any] = {}
        if args.agent:
            overrides["default_agent"] = args.agent
        if args.model:
            overrides["model"] = args.model
        if overrides:
            settings = settings.model_copy(update=overrides)
        # stdout carries the product here (chat text, --json payloads), so logs
        # go to stderr and stay quiet unless VIKRAM_LOG_LEVEL asks otherwise.
        configure_logging(_cli_log_level(settings), stream=sys.stderr)
        init_observability(settings)
        spec = load_agent(settings.default_agent, settings)
        agent = build_agent(spec=spec, settings=settings, approve_all=args.approve_all)

        if prompt is not None:
            result = agent.run_sync(prompt)
            output = str(result.output)
            if output_last_message is not None:
                output_last_message.expanduser().write_text(output, encoding="utf-8")
            if json_output:
                print(json.dumps({"agent": spec.name, "output": output}))
            else:
                print(output)
            return

        import asyncio

        def rebuild_agent(new_settings: Any) -> Any:
            return build_agent(
                spec=spec, settings=new_settings, approve_all=args.approve_all
            )

        asyncio.run(
            run_interactive(
                agent,
                prog_name=spec.name,
                quiet=quiet,
                keep_servers_warm=bool(spec.mcp_servers),
                settings=settings,
                rebuild_agent=rebuild_agent,
                agent_id=spec.agent_dir.name,
            )
        )
    finally:
        os.chdir(old_cwd)


async def run_interactive(
    agent: "VikramAgent",
    *,
    prog_name: str,
    quiet: bool,
    rebuild_agent: Any | None = None,
    agent_id: str | None = None,
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
        [
            "/help",
            "/status",
            "/model",
            "/new",
            "/clear",
            "/copy",
            "/diff",
            "/markdown",
            "/multiline",
            "/exit",
        ]
    )
    context_percent = 0
    context_warned = False

    async with contextlib.AsyncExitStack() as stack:
        # Enter the agent once when MCP servers are configured so they stay
        # connected for the whole session instead of restarting every turn.
        if keep_servers_warm and hasattr(agent, "__aenter__"):
            await stack.enter_async_context(agent)

        _print_banner(
            console,
            prog_name,
            settings,
            model_config=getattr(agent, "model_config", None),
        )

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

            stripped = text.strip()
            if stripped == "/model" or stripped.startswith("/model "):
                agent, settings = await _handle_model_command(
                    stripped[len("/model") :].strip(),
                    agent,
                    settings,
                    console,
                    rebuild_agent=rebuild_agent,
                    stack=stack,
                    keep_servers_warm=keep_servers_warm,
                    agent_id=agent_id,
                    input_async=session.prompt_async,
                )
                continue

            ident_prompt = text.lower().strip().replace(" ", "-")
            if ident_prompt.startswith("/"):
                if ident_prompt in {"/help", "/?", "/h"}:
                    _print_help(console)
                    continue
                if ident_prompt in {"/clear", "/reset", "/new"}:
                    messages = []
                    context_percent = 0
                    context_warned = False
                    console.print("[dim]Conversation history cleared.[/dim]\n")
                    continue
                should_exit, multiline = _handle_slash_command(
                    ident_prompt,
                    messages,
                    multiline,
                    console,
                    prog_name=prog_name,
                    settings=settings,
                    context_percent=context_percent,
                    model_config=getattr(agent, "model_config", None),
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
            except Exception as exc:
                # The console line is for the human; the log carries the
                # traceback so an interactive failure is still diagnosable.
                _log().exception(
                    "cli_turn_failed",
                    agent=prog_name,
                    error_type=type(exc).__name__,
                    prompt_length=len(text),
                )
                console.print(f"\n[red]{type(exc).__name__}[/red]: {exc}")


async def _handle_model_command(
    arg: str,
    agent: Any,
    settings: "VikramSettings" | None,
    console: "Console",
    *,
    rebuild_agent: Any | None,
    stack: contextlib.AsyncExitStack | None = None,
    keep_servers_warm: bool = False,
    agent_id: str | None = None,
    input_async: Any | None = None,
) -> tuple[Any, "VikramSettings" | None]:
    """Show or switch the active model; returns the (agent, settings) to use.

    ``/model`` opens a numbered selector over the configured providers;
    ``/model <provider>`` switches to that provider's configured model;
    ``/model <provider> <model>`` pins both; ``/model <model>`` keeps the
    provider and changes only the model. A successful switch is saved as
    this agent's default (``[agents.<id>]`` in config.toml). A failed switch
    (e.g. missing API key) keeps the current agent. Conversation history is
    preserved.
    """
    from vikram.providers import PROVIDER_IDS, PROVIDERS
    from vikram.settings import resolve_model_selection

    if settings is None or rebuild_agent is None:
        console.print("[dim]/model is not available in this session[/dim]")
        return agent, settings

    current_config = getattr(agent, "model_config", None) or {}
    provider, model = resolve_model_selection(settings)
    provider = current_config.get("provider") or provider
    model = current_config.get("model") or model
    provider_models = getattr(settings, "provider_models", None) or {}

    if not arg:
        if model and provider:
            console.print(f"Current model: {model} [dim]({provider})[/dim]")
        else:
            console.print("[dim]No model configured.[/dim]")
        selectable: list[str] = []
        for provider_id in PROVIDER_IDS:
            configured = provider_models.get(provider_id)
            marker = "*" if provider_id == provider else " "
            if configured:
                selectable.append(provider_id)
                console.print(
                    f"  {marker} {len(selectable)}) {provider_id:<18} {configured}",
                    highlight=False,
                )
            else:
                console.print(
                    f"  {marker}    {provider_id:<18} [dim]not configured[/dim]",
                    highlight=False,
                )
        if not selectable or input_async is None:
            console.print(
                "[dim]Switch with /model <provider>, /model <provider> <model>, "
                "or /model <model>. Run `vikram configure` to add "
                "providers.[/dim]\n"
            )
            return agent, settings
        try:
            choice = str(
                await input_async(
                    f"Select model (1-{len(selectable)}, blank to " "cancel): "
                )
            ).strip()
        except (EOFError, KeyboardInterrupt):
            choice = ""
        if not choice:
            console.print("[dim]Cancelled.[/dim]\n")
            return agent, settings
        if choice.isdigit() and 1 <= int(choice) <= len(selectable):
            # Picking a numbered row selects exactly the model shown on it.
            picked = selectable[int(choice) - 1]
            arg = f"{picked} {provider_models[picked]}"
        elif choice in PROVIDERS:
            arg = choice
        else:
            console.print(f"[dim]Invalid selection: {choice!r}[/dim]\n")
            return agent, settings

    parts = arg.split()
    updates: dict[str, Any]
    if parts[0] in PROVIDERS:
        updates = {
            "model_provider": parts[0],
            "model": parts[1] if len(parts) > 1 else None,
        }
    elif len(parts) > 1:
        console.print(
            f"[red]Unknown provider: {parts[0]!r}.[/red] "
            f"[dim]Valid providers: {', '.join(PROVIDER_IDS)}[/dim]\n"
        )
        return agent, settings
    else:
        suggestion = difflib.get_close_matches(parts[0], PROVIDER_IDS, n=1)
        if suggestion:
            console.print(
                f"[dim]Unknown provider {parts[0]!r} — did you mean "
                f"`/model {suggestion[0]}`? To set a model with that name, "
                f"use /model <provider> {parts[0]}.[/dim]\n"
            )
            return agent, settings
        updates = {"model": parts[0]}

    new_settings = settings.model_copy(update=updates)
    expected_provider, _ = resolve_model_selection(new_settings)
    try:
        new_agent = rebuild_agent(new_settings)
    except Exception as exc:
        # The spec may still supply a model, so a missing model is only
        # known after the rebuild attempt.
        if "model is not configured" in str(exc).lower():
            console.print(
                f"[red]No model configured for {expected_provider}.[/red] "
                f"[dim]Run `vikram configure` or use /model "
                f"{expected_provider} <model>.[/dim]\n"
            )
        else:
            console.print(f"[red]{type(exc).__name__}[/red]: {exc}\n")
        return agent, settings

    if keep_servers_warm and stack is not None and hasattr(new_agent, "__aenter__"):
        # The previous agent's MCP servers stay open on the session stack;
        # they are all cleaned up together when the session exits.
        await stack.enter_async_context(new_agent)

    # The rebuilt agent's model_config is the source of truth (a spec pin may
    # have supplied the model); fall back to settings resolution for doubles.
    new_config = getattr(new_agent, "model_config", None) or {}
    resolved_provider, resolved_model = resolve_model_selection(new_settings)
    new_provider = new_config.get("provider") or resolved_provider
    new_model = new_config.get("model") or resolved_model

    saved_note = ""
    if agent_id and new_provider and new_model:
        try:
            from vikram.config import write_agent_model

            write_agent_model(agent_id, provider=new_provider, model=new_model)
        except Exception as exc:
            console.print(f"[yellow]Could not save model choice: {exc}[/yellow]")
        else:
            saved_note = f" and saved as the {agent_id} default"
            overrides = dict(getattr(new_settings, "agent_overrides", None) or {})
            overrides[agent_id] = {"provider": new_provider, "model": new_model}
            new_settings = new_settings.model_copy(
                update={"agent_overrides": overrides}
            )

    console.print(
        f"[dim]Model set to[/dim] {new_model} [dim]({new_provider}){saved_note} — "
        "conversation history kept[/dim]\n",
        highlight=False,
    )
    return new_agent, new_settings


def _print_banner(
    console: "Console",
    prog_name: str,
    settings: "VikramSettings" | None,
    model_config: dict[str, Any] | None = None,
) -> None:
    # The built agent's model_config is authoritative (spec pins and saved
    # per-agent choices apply there); settings resolution is the fallback.
    provider = (model_config or {}).get("provider")
    model = (model_config or {}).get("model")
    if (not provider or not model) and settings is not None:
        from vikram.settings import resolve_model_selection

        resolved_provider, resolved_model = resolve_model_selection(settings)
        provider = provider or resolved_provider
        model = model or resolved_model
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
        ("/status", "Show the active agent, model, directory, and context usage"),
        ("/model", "Show or switch the model, e.g. /model anthropic"),
        ("/new", "Start a new conversation (also /clear or /reset)"),
        ("/copy", "Copy the last assistant reply (also /cp)"),
        ("/diff", "Show staged, unstaged, and untracked changes"),
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
    *,
    prog_name: str = "vikram",
    settings: "VikramSettings" | None = None,
    context_percent: int = 0,
    model_config: dict[str, Any] | None = None,
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
    if command == "/status":
        _print_status(
            console, prog_name, settings, context_percent, model_config=model_config
        )
        return False, multiline
    if command in {"/copy", "/cp"}:
        text = _last_assistant_text(messages)
        if not text:
            console.print("[yellow]No assistant reply to copy yet.[/yellow]")
            return False, multiline
        error = _copy_to_clipboard(text)
        if error:
            console.print(f"[yellow]Could not copy reply:[/yellow] {error}")
        else:
            console.print("[dim]Copied the last assistant reply.[/dim]")
        return False, multiline
    if command == "/diff":
        from rich.syntax import Syntax

        try:
            diff = _git_diff()
        except RuntimeError as exc:
            console.print(f"[yellow]Could not show changes:[/yellow] {exc}")
            return False, multiline
        if not diff:
            console.print("[dim]Working tree is clean.[/dim]")
        else:
            console.print(Syntax(diff, "diff", theme=CODE_THEME, word_wrap=False))
        return False, multiline
    console.print(f"[dim]Unknown command: {command}[/dim]")
    return False, multiline


def _print_status(
    console: "Console",
    prog_name: str,
    settings: "VikramSettings" | None,
    context_percent: int,
    model_config: dict[str, Any] | None = None,
) -> None:
    from rich.table import Table

    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    table.add_column(style="cyan", no_wrap=True)
    table.add_column()
    table.add_row("Agent", prog_name)
    if settings is not None or model_config:
        # The built agent's model_config is authoritative; settings
        # resolution covers callers without a live agent.
        provider = (model_config or {}).get("provider")
        model = (model_config or {}).get("model")
        if (not provider or not model) and settings is not None:
            from vikram.settings import resolve_model_selection

            resolved_provider, resolved_model = resolve_model_selection(settings)
            provider = provider or resolved_provider
            model = model or resolved_model
        table.add_row("Model", str(model or "not configured"))
        table.add_row("Provider", str(provider or "not configured"))
        if settings is not None and settings.context_window_tokens > 0:
            table.add_row(
                "Context",
                f"{context_percent}% of {settings.context_window_tokens:,} tokens",
            )
    table.add_row("Directory", str(Path.cwd()))
    console.print(table)
    console.print()


def _last_assistant_text(messages: list[Any]) -> str:
    for message in reversed(messages):
        role = (
            message.get("role")
            if isinstance(message, dict)
            else getattr(message, "role", None)
        )
        if str(role).lower() != "assistant":
            continue
        content = (
            message.get("content")
            if isinstance(message, dict)
            else getattr(message, "content", None)
        )
        if isinstance(content, str):
            return content.strip()
        rendered: list[str] = []
        for item in content or []:
            if isinstance(item, str):
                rendered.append(item)
            elif isinstance(item, dict) and item.get("text") is not None:
                rendered.append(str(item["text"]))
            elif getattr(item, "text", None) is not None:
                rendered.append(str(item.text))
        text = "\n".join(rendered).strip()
        if text:
            return text
    return ""


def _copy_to_clipboard(text: str) -> str | None:
    if sys.platform == "darwin":
        candidates = [["pbcopy"]]
    elif sys.platform == "win32":
        candidates = [["clip"]]
    else:
        candidates = [
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ]
    command = next((item for item in candidates if shutil.which(item[0])), None)
    if command is None:
        return "no supported clipboard command was found"
    try:
        subprocess.run(
            command,
            input=text,
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        return " ".join(str(exc).splitlines())
    return None


def _git_diff() -> str:
    status = _run_git("status", "--short")
    unstaged = _run_git("diff", "--no-ext-diff", "--")
    staged = _run_git("diff", "--no-ext-diff", "--cached", "--")
    sections: list[str] = []
    if status:
        sections.append(f"Working tree\n{status}")
    if unstaged:
        sections.append(f"Unstaged changes\n{unstaged}")
    if staged:
        sections.append(f"Staged changes\n{staged}")
    return "\n\n".join(sections)


def _run_git(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError as exc:
        raise RuntimeError("Git is not available") from exc
    if result.returncode:
        detail = result.stderr.strip() or f"git {' '.join(args)} failed"
        raise RuntimeError(detail)
    return result.stdout.rstrip()


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


def _truncate(text: str, limit: int = 200) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


if __name__ == "__main__":
    main()
