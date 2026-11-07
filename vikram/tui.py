"""One terminal application owns input, streaming output and modal interactions."""

import asyncio
import contextlib
import io
import json
import math
import os
import re
import shlex
import sqlite3
import subprocess
import tempfile
import time
import warnings
from pathlib import Path

from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Float, FloatContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Dialog, TextArea
from pydantic_ai import PydanticAIDeprecationWarning
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from vikram.errors import reply_error
from vikram.interaction import AskUserResponse, PlanReview, answer_from_text
from vikram.model_names import format_model_name
from vikram.providers import ModelRef
from vikram.terminal import ACCENT, render_reply, terminal_text

COMMANDS = {
    "help": "Commands and keyboard shortcuts",
    "hotkeys": "Keyboard shortcuts",
    "model": "Search installed local models and connected OpenRouter models",
    "default-model": "Choose the default model for new CLI and desktop conversations",
    "thinking": "Select this model's supported thinking control",
    "scoped-models": "Choose models for cycling",
    "settings": "Appearance and terminal preferences",
    "new": "Start a conversation",
    "resume": "Search saved conversations",
    "name": "Rename this conversation",
    "session": "Session, workspace and context information",
    "tree": "Navigate retained conversation branches (does not undo files)",
    "fork": "Fork from an earlier message",
    "clone": "Copy this conversation",
    "compact": "Summarize earlier context without deleting the transcript",
    "attach": "Attach a document or image",
    "attachments": "Inspect or remove pending attachments",
    "paste-image": "Attach a clipboard image (Ctrl+V)",
    "copy": "Copy the last answer",
    "export": "Export locally as Markdown, HTML or JSONL",
    "import": "Import a local JSONL conversation",
    "queue": "Edit or remove queued messages",
    "plan": "Toggle plan mode",
    "trace": "Show or hide thinking returned in this process",
    "activity": "Show or hide tool details",
    "search": "Search the transcript",
    "workspace": "Choose the coding workspace",
    "trust": "Inspect or change this workspace's trust",
    "login": "Connect OpenRouter",
    "logout": "Remove saved OpenRouter credentials",
    "mcp": "Manage MCP servers and inspect tools",
    "extensions": "Manage Python extension packages",
    "packages": "Manage Python extension packages and their settings",
    "commands": "Discover and run installed extension commands",
    "reload": "Reload trusted project resources and preferences",
    "quit": "Close Vikram",
    "exit": "Close Vikram",
}


class Commands(Completer):
    def __init__(self, ui):
        self.ui = ui

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith("/") and " " not in text:
            for name, description in self.ui.command_catalog().items():
                if name.startswith(text[1:]):
                    yield Completion("/" + name, -len(text), display_meta=description)
        else:
            match = re.search(r"(?:^|\s)(@[^\n]*)$", text)
            if not match:
                return
            prefix = match[1]
            raw = prefix[1:]
            try:
                decoded = shlex.split(raw)
            except ValueError:
                try:
                    decoded = shlex.split(raw + raw[:1])
                except ValueError:
                    return
            fragment = decoded[0] if len(decoded) == 1 else raw
            path = Path(fragment or ".").expanduser()
            directory = path if not fragment or fragment.endswith("/") else path.parent
            partial = "" if not fragment or fragment.endswith("/") else path.name
            try:
                for entry in sorted(
                    directory.iterdir(), key=lambda p: p.name.casefold()
                ):
                    if entry.name.casefold().startswith(partial.casefold()):
                        value = str(entry) + ("/" if entry.is_dir() else "")
                        yield Completion(
                            "@" + shlex.quote(value),
                            -len(prefix),
                            display=entry.name,
                            display_meta="Folder" if entry.is_dir() else "Attach file",
                        )
            except OSError:
                return


class TranscriptLexer(Lexer):
    def __init__(self):
        self.lines = []

    def lex_document(self, document):
        lines = self.lines
        return lambda number: lines[number] if number < len(lines) else []


class TerminalApp:
    """A terminal adapter. Session and tool services own execution and storage."""

    def __init__(
        self, session, *, settings=None, input=None, output=None, full_screen=True
    ):
        self.session = session
        self.settings = settings
        self.full_screen = full_screen
        self.ready = asyncio.Event()
        self.idle = asyncio.Event()
        self.idle.set()
        self.messages = []
        self.notice = "Ctrl+L models · /help commands · Ctrl+D quit"
        self.show_thinking = settings.get("show_thinking", False) if settings else False
        self.show_tools = settings.get("show_tools", False) if settings else False
        self.task = None
        self.dialog = None
        self.dialog_future = None
        self.dialog_is_interaction = False
        self.dialog_accept = None
        self.dialog_move = None
        self.dialog_preview = None
        self.started = 0.0
        self.models = []
        self.extension_commands = {}
        self.resources = None
        if settings is not None:
            from vikram.resources import ProjectResources

            self.resources = ProjectResources(settings)
            self.resources.reload()
        self.cloud_enabled = session.model.startswith("openrouter:")
        self.cache = {}
        self.printed = 0
        self._print_lock = asyncio.Lock()
        self._redraw_handle = None
        self._draft_handle = None
        self._pending_submission = None
        self.lexer = TranscriptLexer()
        self.transcript = TextArea(
            read_only=True,
            scrollbar=True,
            wrap_lines=True,
            lexer=self.lexer,
            height=None if full_screen else 10,
        )
        self.editor = TextArea(
            text=(
                session.store.chat(session.chat_id)["draft"]
                if getattr(session, "store", None) is not None
                else ""
            ),
            height=lambda: min(8, max(2, self.editor.text.count("\n") + 1)),
            prompt="› ",
            multiline=True,
            wrap_lines=True,
            completer=Commands(self),
            complete_while_typing=True,
            history=InMemoryHistory(),
        )
        self.header = Window(FormattedTextControl(self._header), height=1)
        self.footer = Window(FormattedTextControl(self._footer), height=1)
        self.hint = Window(
            FormattedTextControl(lambda: terminal_text(self.notice)), height=2
        )
        self.queued = Window(
            FormattedTextControl(self._queue_text),
            height=lambda: 1 if self._queue_text() else 0,
        )
        self.container = FloatContainer(
            HSplit(
                [
                    self.header,
                    self.transcript,
                    self.queued,
                    self.hint,
                    self.editor,
                    self.footer,
                ]
            ),
            floats=[],
        )
        self.app = Application(
            layout=Layout(self.container, focused_element=self.editor),
            key_bindings=self._bindings(),
            input=input,
            output=output,
            full_screen=full_screen,
            mouse_support=full_screen,
            style=self._style(),
            refresh_interval=0.2,
            min_redraw_interval=0.05,
        )
        self._load_transcript()
        self.editor.buffer.cursor_position = len(self.editor.text)
        self.editor.buffer.on_text_changed += self._draft_changed

    def _draft_changed(self, _=None):
        if self._draft_handle is not None:
            self._draft_handle.cancel()
        if getattr(self.session, "store", None) is not None:
            try:
                self._draft_handle = asyncio.get_running_loop().call_later(
                    0.25, self.save_draft
                )
            except RuntimeError:
                self._draft_handle = None

    def _submission_started(self):
        pending = self._pending_submission
        if pending is None:
            return False
        if not pending["started"]:
            pending["started"] = len(self.session.turns) > pending["turns"]
            if getattr(self.session, "store", None) is not None:
                pending["started"] |= (
                    self.session.store.chat(self.session.chat_id)["active_leaf"]
                    != pending["leaf"]
                )
        return pending["started"]

    def save_draft(self):
        """Flush the editor before releasing ownership; preflight input stays recoverable."""
        if self._draft_handle is not None:
            self._draft_handle.cancel()
            self._draft_handle = None
        store = getattr(self.session, "store", None)
        if store is None:
            return
        try:
            text = self.editor.text
            pending = self._pending_submission
            preflight = pending is not None and not self._submission_started()
            if preflight:
                text = "\n\n".join(part for part in (pending["prompt"], text) if part)
            if len(text) > 1_000_000:
                self.notice = "Draft exceeds the save limit. Shorten it to one million characters; the editor retains your text."
            else:
                saved = store.chat(self.session.chat_id)
                if saved["draft"] != text:
                    store.save_draft(
                        self.session.chat_id, text, saved["draft_revision"] + 1
                    )
            # Session.begin consumes its own prompt atomically. Check again after
            # that boundary so a newer editor draft is not lost with it.
            if preflight:
                self._draft_changed()
        except (ValueError, OSError, sqlite3.Error):
            self.notice = (
                "The draft could not be saved. Your text remains in the editor."
            )

    def _begin_submission(self, prompt):
        store = getattr(self.session, "store", None)
        self._pending_submission = {
            "prompt": prompt,
            "started": False,
            "turns": len(self.session.turns),
            "leaf": store.chat(self.session.chat_id)["active_leaf"] if store else None,
        }
        self.save_draft()

    def _restore_unsent(self):
        pending = self._pending_submission
        if pending is None or self._submission_started():
            return
        self._pending_submission = None
        if not self.editor.text:
            self.editor.text = pending["prompt"]
            self.editor.buffer.cursor_position = len(self.editor.text)
        else:
            try:
                self.session.queue_input(pending["prompt"], kind="followup")
            except ValueError:
                self.editor.text += "\n\n" + pending["prompt"]

    def _style(self):
        theme = self.settings.get("theme", "system") if self.settings else "system"
        colors = {"dialog": "bg:#252525 #eeeeee", "dialog.body": "bg:#252525 #eeeeee"}
        if theme == "light":
            colors = {
                "dialog": "bg:#eeeeee #222222",
                "dialog.body": "bg:#eeeeee #222222",
            }
        custom = (
            self.resources.theme(theme)
            if self.resources and theme not in {"system", "dark", "light"}
            else {}
        )
        try:
            return Style.from_dict(
                {
                    "header": f"bold {ACCENT}",
                    "footer": "dim",
                    "selected": f"bg:{ACCENT} #000000",
                    **colors,
                    **custom,
                }
            )
        except ValueError:
            self.notice = (
                "This theme contains an unsupported style; using system colors."
            )
            return Style.from_dict(colors)

    def command_catalog(self):
        commands = dict(COMMANDS)
        if self.resources:
            commands.update(
                (r.name, r.description or "Expand prompt template")
                for r in self.resources.catalog.templates
                if r.name not in commands
            )
            commands.update(
                ("skill:" + r.name, r.description or "Use this skill")
                for r in self.resources.catalog.skills
            )
        commands.update(
            (name, item.get("description", "Extension command"))
            for name, item in self.extension_commands.items()
        )
        return commands

    def apply_preferences(self):
        if self.settings:
            self.show_thinking = self.settings.get("show_thinking", False)
            self.show_tools = self.settings.get("show_tools", False)
        self.app.style = self._style()
        self.app.key_bindings = self._bindings()
        self.cache.clear()
        self.redraw()

    def schedule(self, coroutine):
        async def guarded():
            try:
                await coroutine
            except (ValueError, OSError) as error:
                self.notice = terminal_text(str(error))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.notice = reply_error(
                    error, provider=ModelRef.parse(self.session.model).provider
                )
            finally:
                self.app.invalidate()

        return self.app.create_background_task(guarded())

    def _header(self):
        model = terminal_text(
            format_model_name(ModelRef.parse(self.session.model).model_id)
        )
        where = (
            "OpenRouter · cloud"
            if self.session.model.startswith("openrouter:")
            else "Ollama · local"
        )
        return [
            ("class:header", f" Vikram  ·  {model}  ·  {where}  "),
            ("", "PLAN" if self.session.mode == "plan" else ""),
        ]

    def _footer(self):
        context = (
            self.session.context_state()
            if hasattr(self.session, "context_state")
            else {}
        )
        usage = context.get("tokens", context.get("estimated_tokens", "?"))
        limit = context.get("context_limit", context.get("context_length")) or "?"
        elapsed = (
            f" · {time.monotonic() - self.started:.0f}s · Esc stop"
            if not self.idle.is_set()
            else ""
        )
        actual = context.get("last_usage") or {}
        usage_detail = ""
        if "input_tokens" in actual and "output_tokens" in actual:
            usage_detail = (
                f" · last {actual['input_tokens']} in / {actual['output_tokens']} out"
            )
        try:
            cost = float(actual.get("cost_usd"))
        except (ValueError, TypeError):
            cost = None
        if cost is not None and math.isfinite(cost) and cost >= 0:
            estimate = "~" if actual.get("cost_estimated") else ""
            usage_detail += f" · {estimate}${cost:.4f}"
        return [
            (
                "class:footer",
                terminal_text(
                    f" {getattr(self.session, 'workspace', None) or Path.cwd()} · thinking {self.session.thinking} · context ~{usage}/{limit}{usage_detail}{elapsed}"
                ),
            )
        ]

    def _queue_text(self):
        items = getattr(self.session, "queued_inputs", [])
        if not items:
            return ""
        return terminal_text(
            "Queued · "
            + " | ".join(
                f"{item.get('kind', 'followup')}: {item.get('text', '')[:70]}"
                for item in items
            )
            + " · /queue to edit"
        )

    def _bindings(self):
        keys = KeyBindings()

        schedule = self.schedule

        @keys.add("enter", eager=True)
        def enter(event):
            if self.dialog:
                self.dialog_accept()
            else:
                text = self.editor.text
                self.save_draft()
                self.editor.buffer.append_to_history()
                self.editor.text = ""
                schedule(self.submit(text))

        @keys.add("c-j")
        def newline(event):
            event.current_buffer.insert_text("\n")

        @keys.add("escape", "enter")
        def followup(event):
            if self.dialog:
                self.dialog_accept()
                return
            self.save_draft()
            text, self.editor.text = self.editor.text, ""
            schedule(self.submit(text, kind="followup"))

        @keys.add("escape", eager=False)
        @keys.add("c-c")
        def cancel(event):
            if self.dialog:
                self._finish_dialog(None)
            elif not self.idle.is_set():
                schedule(self.stop())
            else:
                self.editor.text = ""

        @keys.add("c-d")
        def quit_app(event):
            if self.dialog:
                self._finish_dialog(None)
            elif not self.editor.text and self.idle.is_set():
                self.app.exit()

        @keys.add("c-l")
        @keys.add("f2")
        def models(event):
            schedule(self.command("model", ""))

        @keys.add("c-t")
        def thinking(event):
            self.show_thinking = not self.show_thinking
            self.redraw()

        @keys.add("c-o")
        def tools(event):
            self.show_tools = not self.show_tools
            self.redraw()

        @keys.add("c-p")
        def cycle(event):
            schedule(self.cycle_model())

        @keys.add("s-tab")
        def cycle_thinking(event):
            schedule(self.command("thinking", ""))

        @keys.add("c-g")
        def external(event):
            schedule(self.external_editor())

        @keys.add("c-x")
        def copy(event):
            schedule(self.command("copy", ""))

        @keys.add("c-f")
        def search(event):
            schedule(self.command("search", ""))

        @keys.add("c-v")
        def paste_image(event):
            schedule(self.command("paste-image", ""))

        @keys.add("up", filter=Condition(lambda: self.dialog_move is not None))
        def up(event):
            if self.dialog_preview and self.app.layout.has_focus(self.dialog_preview):
                self.dialog_preview.buffer.cursor_up()
            else:
                self.dialog_move(-1)

        @keys.add("down", filter=Condition(lambda: self.dialog_move is not None))
        def down(event):
            if self.dialog_preview and self.app.layout.has_focus(self.dialog_preview):
                self.dialog_preview.buffer.cursor_down()
            else:
                self.dialog_move(1)

        @keys.add("pageup")
        def page_up(event):
            if self.dialog_preview and self.app.layout.has_focus(self.dialog_preview):
                self.dialog_preview.buffer.cursor_up(8)
            elif self.dialog_move:
                self.dialog_move(-8)
            else:
                self.transcript.buffer.cursor_up(15)

        @keys.add("pagedown")
        def page_down(event):
            if self.dialog_preview and self.app.layout.has_focus(self.dialog_preview):
                self.dialog_preview.buffer.cursor_down(8)
            elif self.dialog_move:
                self.dialog_move(8)
            else:
                self.transcript.buffer.cursor_down(15)

        @keys.add("escape", "up")
        def restore(event):
            self._restore_queue()

        handlers = {
            "send": enter,
            "newline": newline,
            "cancel": cancel,
            "models": models,
            "thinking": thinking,
            "tools": tools,
            "search": search,
            "editor": external,
            "copy": copy,
            "quit": quit_app,
            "clear": lambda event: setattr(self.editor, "text", ""),
            "complete": lambda event: event.current_buffer.start_completion(),
            "queue": lambda event: schedule(self.command("queue", "")),
        }
        configured = (
            dict(self.resources.catalog.keybindings or {}) if self.resources else {}
        )
        configured.update(self.settings.get("keybindings", {}) if self.settings else {})
        for binding, action in configured.items():
            # Resource catalogs use key -> action; retain older action -> key settings.
            if binding in handlers or binding in COMMANDS:
                binding, action = action, binding
            if not isinstance(binding, str) or not isinstance(action, str):
                continue
            try:
                if action in handlers:
                    handler = handlers[action]
                elif action in COMMANDS:
                    handler = lambda event, command=action: schedule(
                        self.command(command, "")
                    )
                else:
                    continue
                keys.add(*binding.split())(handler)
            except ValueError:
                self.notice = (
                    f"Unsupported key binding for {action}; using built-in shortcuts."
                )

        return keys

    def _load_transcript(self):
        self.printed = 0
        self.cache.clear()
        self.messages.clear()
        for turn in getattr(self.session, "turns", []):
            text = "\n".join(
                (
                    x
                    if isinstance(x, str)
                    else f"[Image attachment · {getattr(x, 'media_type', 'binary')} · terminal preview unavailable]"
                )
                for x in turn.get("prompt", [])
            )
            self.messages.append({"role": "user", "text": text})
            if turn.get("answer") is not None:
                self.messages.append(
                    {
                        "role": "assistant",
                        "text": turn["answer"],
                        "activity": turn.get("activity", []),
                        "status": "complete",
                    }
                )
        self.redraw()

    def transcript_text(self):
        return "\n\n".join(
            terminal_text(item.get("text", "")) for item in self.messages
        )

    def redraw(self):
        if self._redraw_handle is not None:
            self._redraw_handle.cancel()
            self._redraw_handle = None
        width = (
            max(20, self.app.output.get_size().columns - 3)
            if hasattr(self, "app")
            else 80
        )
        lines = []
        for item in (
            self.messages if self.full_screen else self.messages[self.printed :]
        ):
            item = dict(item)
            for field in ("text", "reasoning"):
                if len(item.get(field, "")) > 32000:
                    item[field] = (
                        item[field][:30000]
                        + "\n\n[Long display shortened; /copy or /export keeps the full text.]\n\n"
                        + item[field][-2000:]
                    )
            key = (
                json.dumps(item, sort_keys=True, default=str),
                width,
                self.show_thinking,
                self.show_tools,
            )
            fragments = self.cache.get(key)
            if fragments is None:
                output = io.StringIO()
                console = Console(
                    file=output,
                    width=width,
                    force_terminal=True,
                    color_system="truecolor",
                )
                if item["role"] == "user":
                    console.print(
                        Text(
                            "You › " + terminal_text(item.get("text", "")), style="bold"
                        )
                    )
                else:
                    console.print(
                        render_reply(
                            item,
                            show_thinking=self.show_thinking,
                            status=item.get("status", "complete"),
                        )
                    )
                    if self.show_tools:
                        for activity in item.get("activity", []):
                            detail = (
                                activity.get("diff")
                                or activity.get("output")
                                or activity.get("error")
                                or activity.get("code")
                                or ""
                            )
                            console.print(
                                Text(terminal_text(str(detail)[:32000]), style="dim")
                            )
                fragments = list(to_formatted_text(ANSI(output.getvalue())))
                self.cache[key] = fragments
                if len(self.cache) > 256:
                    self.cache.pop(next(iter(self.cache)))
            current = []
            for style, text, *_ in fragments:
                parts = text.split("\n")
                for index, part in enumerate(parts):
                    current.append((style, part))
                    if index < len(parts) - 1:
                        lines.append(current)
                        current = []
            lines.extend([current, []])
        self.lexer.lines = lines
        text = "\n".join("".join(part for _, part in line) for line in lines)
        buffer = self.transcript.buffer
        old_position = buffer.cursor_position
        following = old_position >= max(0, len(buffer.text) - 1)
        if buffer.text != text:
            from prompt_toolkit.document import Document

            buffer.set_document(
                Document(
                    text, len(text) if following else min(old_position, len(text))
                ),
                bypass_readonly=True,
            )
        if hasattr(self, "app"):
            self.app.invalidate()

    def request_redraw(self):
        if self._redraw_handle is None:
            self._redraw_handle = asyncio.get_running_loop().call_later(
                0.05, self.redraw
            )

    async def submit(self, text, *, kind="steer"):
        text = text.strip()
        if not text:
            return
        try:
            if text.startswith("/"):
                command, _, argument = text[1:].partition(" ")
                await self.command(command, argument.strip())
            elif not self.idle.is_set():
                self.session.queue_input(text, kind=kind)
                self.app.invalidate()
            elif text.startswith("!"):
                await self.shell(
                    text[2:] if text.startswith("!!") else text[1:],
                    include=not text.startswith("!!"),
                )
            else:
                from vikram.terminal_commands import (
                    attach_paths,
                    attachment_prefix,
                    dropped_paths,
                )

                if text.startswith("@"):
                    paths, text = attachment_prefix(text)
                    await attach_paths(self, paths)
                    text = text or "Please review the attached files."
                elif paths := dropped_paths(text):
                    await attach_paths(self, paths)
                    return
                self.start_reply(text)
        except (ValueError, OSError) as error:
            if text and not text.startswith("/"):
                if not self.editor.text:
                    self.editor.text = text
                else:
                    self.session.queue_input(text, kind="followup")
                self.save_draft()
            self.notice = terminal_text(str(error))
            self.app.invalidate()

    def start_reply(self, text):
        if not self.idle.is_set():
            raise ValueError("A reply is already running.")
        self._begin_submission(text)
        self.idle.clear()

        async def consume():
            prompt = text
            try:
                while prompt:
                    if self._pending_submission is None:
                        self._begin_submission(prompt)
                    self.started = time.monotonic()
                    self.messages.append({"role": "user", "text": prompt})
                    item = {
                        "role": "assistant",
                        "text": "",
                        "status": "running",
                        "phase": "loading",
                    }
                    self.messages.append(item)
                    self.redraw()
                    try:
                        async with contextlib.aclosing(
                            self.session.reply(prompt, interaction=self)
                        ) as events:
                            async for event in events:
                                if event.get("notice"):
                                    self.notice = event["notice"]
                                else:
                                    if not self._pending_submission["started"]:
                                        self._pending_submission["started"] = True
                                        self.save_draft()
                                    item.update(event)
                                    self.request_redraw()
                        item["status"] = "complete"
                    except asyncio.CancelledError:
                        self._restore_unsent()
                        item["status"] = "stopped"
                        self.notice = "Stopped. Partial answer is excluded from future context; completed tool effects remain."
                        break
                    except Exception as error:
                        self._restore_unsent()
                        item["status"] = "failed"
                        self.notice = (
                            str(error)
                            if isinstance(error, ValueError)
                            else reply_error(
                                error,
                                provider=ModelRef.parse(self.session.model).provider,
                            )
                        )
                        break
                    self.redraw()
                    self._pending_submission = None
                    self.save_draft()
                    if not self.full_screen and self.app.is_running:
                        await self.print_regular()
                    next_message = self.session.take_followup()
                    if next_message:
                        prompt = next_message
                    elif self.session.queued_inputs:
                        queued = self.session.queued_inputs[0]
                        self.session.remove_queued(queued["id"])
                        prompt = queued["text"]
                    else:
                        prompt = None
            except Exception as error:
                self.notice = reply_error(
                    error, provider=ModelRef.parse(self.session.model).provider
                )
            finally:
                self._pending_submission = None
                self.save_draft()
                if self.dialog_future is not None and self.dialog_is_interaction:
                    self._finish_dialog(None)
                self.idle.set()
                self.redraw()

                if not self.full_screen and self.app.is_running:
                    await self.print_regular()

        self.task = asyncio.create_task(consume())

    async def wait_idle(self):
        await self.idle.wait()
        if self.task is not None:
            await asyncio.shield(self.task)

    def _restore_queue(self):
        pending = (
            self.session.drain_queued()
            if hasattr(self.session, "drain_queued")
            else (
                self.session.restore_queued()
                if hasattr(self.session, "restore_queued")
                else []
            )
        )
        if isinstance(pending, str):
            pending = [pending]
        text = "\n\n".join(
            item if isinstance(item, str) else item.get("text", "") for item in pending
        )
        if text:
            self.editor.text = "\n\n".join(
                part for part in (self.editor.text, text) if part
            )

    async def stop(self):
        if self.task and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        self._restore_unsent()
        self._pending_submission = None
        self.idle.set()
        self._restore_queue()
        self.save_draft()
        self.app.invalidate()

    def _finish_dialog(self, value):
        future = self.dialog_future
        self.container.floats.clear()
        self.dialog = self.dialog_future = self.dialog_accept = self.dialog_move = None
        self.dialog_preview = None
        self.dialog_is_interaction = False
        self.app.layout.focus(self.editor)
        if future is not None and not future.done():
            future.set_result(value)
        self.app.invalidate()

    async def _modal(
        self, title, body, focus, accept, move=None, *, interaction=False, preview=None
    ):
        if self.dialog_future:
            raise ValueError("Finish the open dialog first.")
        future = asyncio.get_running_loop().create_future()
        self.dialog_future = future
        self.dialog_is_interaction = interaction
        self.dialog_accept = accept
        self.dialog_move = move
        self.dialog_preview = preview
        self.dialog = Dialog(
            title=title,
            body=body,
            width=Dimension(min=20, preferred=88, max=88),
            with_background=True,
        )
        self.container.floats.append(Float(content=self.dialog))
        self.app.layout.focus(focus)
        self.app.invalidate()
        try:
            return await future
        finally:
            if self.dialog_future is future:
                self._finish_dialog(None)

    async def read(self, title, default="", *, password=False, interaction=False):
        field = TextArea(text=default, multiline=False, password=password)
        body = HSplit(
            [
                field,
                Window(
                    FormattedTextControl("Enter accepts · Escape cancels"), height=1
                ),
            ]
        )
        return await self._modal(
            title,
            body,
            field,
            lambda: self._finish_dialog(field.text),
            interaction=interaction,
        )

    async def select(self, title, options, *, interaction=False, details=""):
        """Searchable keyboard picker. Options are (value, label) pairs."""
        field = TextArea(multiline=False)
        position = [0]

        def matches():
            query = field.text.casefold()
            return [
                option
                for option in options
                if all(word in option[1].casefold() for word in query.split())
            ]

        def content():
            values = matches()
            if not values:
                return [("", "No matches")]
            position[0] = min(position[0], len(values) - 1)
            start = max(0, position[0] - 7)
            return [
                (
                    "class:selected" if index == position[0] else "",
                    terminal_text(label) + "\n",
                )
                for index, (_, label) in enumerate(values)
                if start <= index < start + 12
            ]

        def move(delta):
            position[0] = max(0, min(len(matches()) - 1, position[0] + delta))
            self.app.invalidate()

        def accept():
            values = matches()
            if values:
                self._finish_dialog(values[min(position[0], len(values) - 1)][0])

        field.buffer.on_text_changed += lambda _: position.__setitem__(0, 0)
        preview = (
            TextArea(
                text=terminal_text(details),
                read_only=True,
                scrollbar=True,
                wrap_lines=True,
                height=8,
            )
            if details
            else None
        )
        preview_keys = KeyBindings()

        @preview_keys.add("tab")
        def focus_preview(event):
            self.app.layout.focus(
                field if self.app.layout.has_focus(preview) else preview
            )

        body = HSplit(
            ([preview] if preview else [])
            + [
                field,
                Window(
                    FormattedTextControl(content), height=max(1, min(12, len(options)))
                ),
                Window(
                    FormattedTextControl(
                        ("Tab preview · " if preview else "")
                        + "Type to search · ↑↓ select · Enter accepts · Escape cancels"
                    ),
                    height=1,
                ),
            ],
            key_bindings=preview_keys if preview else None,
        )
        return await self._modal(
            title, body, field, accept, move, interaction=interaction, preview=preview
        )

    async def ask(self, request):
        answers = []
        for question in request.questions:
            options = [
                (
                    str(index),
                    option.label
                    + (" · " + option.description if option.description else ""),
                )
                for index, option in enumerate(question.options, 1)
            ]
            options.extend([("custom", "Write an answer"), ("", "Skip this question")])
            if question.multi_select:
                chosen = []
                while True:
                    selected = await self.select(
                        question.question + " · select several, then Done",
                        [("done", f"Done · {len(chosen)} selected")]
                        + [
                            (key, ("✓ " if key in chosen else "  ") + label)
                            for key, label in options
                        ],
                        interaction=True,
                    )
                    if selected in (None, "", "custom", "done"):
                        if selected == "done":
                            selected = ",".join(chosen)
                        break
                    if selected in chosen:
                        chosen.remove(selected)
                    else:
                        chosen.append(selected)
            else:
                selected = await self.select(
                    question.question, options, interaction=True
                )
            if selected == "custom":
                selected = await self.read(question.question, interaction=True)
            answer = answer_from_text(question, selected or "")
            if answer is not None:
                answers.append(answer)
        return AskUserResponse(answers=answers)

    async def review_plan(self, request):
        self.messages.append(
            {"role": "assistant", "text": request.plan, "status": "complete"}
        )
        self.redraw()
        selected = await self.select(
            "Review the proposed plan",
            [
                ("approve", "Approve and execute"),
                ("revise", "Request changes"),
                ("dismiss", "Keep planning"),
            ],
            interaction=True,
        )
        if selected == "revise":
            feedback = await self.read("What should change?", interaction=True)
            return (
                PlanReview(decision="revise", feedback=feedback)
                if feedback
                else PlanReview(decision="dismiss")
            )
        return PlanReview(decision=selected or "dismiss")

    async def approve(self, request):
        selected = await self.select(
            terminal_text(request.summary),
            [(False, "Decline"), (True, "Allow: " + terminal_text(request.reason))],
            interaction=True,
            details=(
                f"Reason: {request.reason}\n\nArguments:\n{request.details}"
                if getattr(request, "details", "")
                else ""
            ),
        )
        return selected is True

    async def refresh_models(self):
        try:
            self.models = await self.session.catalog.refresh(
                include_cloud=self.cloud_enabled
            )
        except TypeError:
            self.models = await self.session.catalog.refresh()
        return self.models

    async def change_model(self, name):
        if not self.idle.is_set():
            raise ValueError(
                "Stop the reply before changing models. The current draft is preserved."
            )
        if name.startswith("openrouter:") and not self.session.model.startswith(
            "openrouter:"
        ):
            approved = await self.select(
                "OpenRouter sends this conversation's active context to the cloud",
                [
                    (False, "Keep local inference"),
                    (True, "Use and remember this OpenRouter model"),
                ],
                details="This model will also be the default for future CLI and desktop conversations on this Mac.",
            )
            if approved is not True:
                return
        self.session.model = name
        self.session.thinking = "on"
        self._save_choice()
        if self.settings is not None:
            self.settings.set("model", name)
        self.notice = "Selected and saved default: " + name
        self.app.invalidate()

    async def pick_model(self, *, default_only=False, models=None):
        while True:
            if models is None:
                try:
                    models = await self.refresh_models()
                except ValueError as error:
                    self.notice = str(error)
                    models = []
            options = []
            for model in models:
                capabilities = ", ".join(
                    cap
                    for key, cap in (
                        ("tools", "tools"),
                        ("vision", "images"),
                        ("can_think", "thinking"),
                    )
                    if model.get(key)
                )
                label = model["name"] + (" · " + capabilities if capabilities else "")
                if not model["available"]:
                    label += " · unavailable: " + str(
                        model.get("reason") or "Cannot use this model"
                    )
                options.append((model["name"], label))
            options.append(("__refresh__", "Refresh installed models"))
            selected = await self.select(
                (
                    "Default model · new CLI and desktop conversations"
                    if default_only
                    else "Choose a model · selection becomes the default"
                ),
                options,
            )
            if selected == "__refresh__":
                models = None
                continue
            if selected is None:
                return
            model = next((item for item in models if item["name"] == selected), None)
            if model is None or not model["available"]:
                self.notice = (
                    model.get("reason") if model else "Choose an available model."
                )
                continue
            if default_only:
                if ModelRef.parse(selected).is_cloud:
                    approved = await self.select(
                        "OpenRouter default · future conversations go to the cloud",
                        [(False, "Cancel"), (True, "Save default")],
                    )
                    if approved is not True:
                        return
                self.settings.set("model", selected)
                self.notice = "Saved app default: " + selected
            else:
                await self.change_model(selected)
            return

    def _save_choice(self):
        store = getattr(self.session, "store", None)
        if store:
            from vikram.models import thinking_selection

            chat = store.chat(self.session.chat_id)
            choices = chat["thinking"]
            choices[self.session.model] = thinking_selection(self.session.thinking)
            store.update_chat(
                self.session.chat_id,
                model=self.session.model,
                thinking=choices,
                mode=self.session.mode,
            )

    async def cycle_model(self):
        models = [
            item["name"] for item in await self.refresh_models() if item["available"]
        ]
        scope = self.settings.get("models", []) if self.settings else []
        models = [
            name
            for name in models
            if not scope
            or any(
                ModelRef.parse(name) == ModelRef.parse(item) for item in scope if item
            )
        ]
        if models:
            index = next(
                (
                    i + 1
                    for i, name in enumerate(models)
                    if ModelRef.parse(name) == ModelRef.parse(self.session.model)
                ),
                0,
            )
            await self.change_model(models[index % len(models)])

    async def command(self, name, argument):
        if name not in COMMANDS and self.resources and name in self.command_catalog():
            from vikram.terminal_commands import resource_prompt

            prompt = resource_prompt(self, name, argument)
            if prompt is not None:
                if self.idle.is_set():
                    self.start_reply(prompt)
                else:
                    self.session.queue_input(prompt, kind="steer")
                return
        if name in self.extension_commands or name.startswith("ext__"):
            from vikram.terminal_commands import extension_command

            await extension_command(self, name, argument)
        elif name not in COMMANDS:
            self.notice = f"Unknown command /{name}. Type /help for commands."
            return
        elif name in {"help", "hotkeys"}:
            self.messages.append(
                {
                    "role": "assistant",
                    "text": "\n".join(
                        f"- `/{key}` — {value}"
                        for key, value in self.command_catalog().items()
                    )
                    + "\n\nEnter send/steer · Alt+Enter follow-up · Ctrl+J newline · Ctrl+L model · Ctrl+P cycle · Ctrl+G external editor · Ctrl+V clipboard image · Escape stop · Ctrl+D quit",
                    "status": "complete",
                }
            )
            self.redraw()
        elif name in {"quit", "exit"}:
            await self.stop()
            self.app.exit()
        elif name in {"trace", "activity"}:
            if name == "trace":
                self.show_thinking = not self.show_thinking
            else:
                self.show_tools = not self.show_tools
            self.redraw()
        elif name == "plan":
            if not self.idle.is_set():
                raise ValueError("Stop the reply before changing plan mode.")
            self.session.mode = "normal" if self.session.mode == "plan" else "plan"
            self._save_choice()
            self.app.invalidate()
        elif name in {"model", "default-model"}:
            if not self.idle.is_set():
                raise ValueError(
                    "Stop the reply before changing models. Your draft is preserved."
                )
            if argument and name == "model":
                models = await self.refresh_models()
                selected = argument
                model = next(
                    (
                        item
                        for item in models
                        if ModelRef.parse(item["name"]) == ModelRef.parse(selected)
                    ),
                    None,
                )
                if not model or not model["available"]:
                    raise ValueError(
                        model.get("reason") if model else "Choose an available model."
                    )
                await self.change_model(selected)
            else:
                await self.pick_model(default_only=name == "default-model")
        elif name == "thinking":
            models = await self.refresh_models()
            model = next(
                (
                    item
                    for item in models
                    if ModelRef.parse(item["name"])
                    == ModelRef.parse(self.session.model)
                ),
                {},
            )
            values = model.get("thinking", [])
            options = [("on", "On · model default")]
            options += [
                (
                    "off" if value is False else "on" if value is True else value,
                    str(value),
                )
                for value in values
            ]
            choice = argument or await self.select("Thinking", options)
            if choice:
                if choice not in [value for value, _ in options]:
                    raise ValueError(
                        "That thinking control is not supported by this model."
                    )
                self.session.thinking = choice
                self._save_choice()
        elif name == "copy":
            from vikram.coding import child_environment

            text = (
                self.transcript.buffer.copy_selection().text
                if self.transcript.buffer.selection_state
                else next(
                    (
                        item["text"]
                        for item in reversed(self.messages)
                        if item["role"] == "assistant"
                    ),
                    "",
                )
            )
            await asyncio.to_thread(
                subprocess.run,
                ["pbcopy"],
                input=text.encode(),
                check=True,
                env=child_environment(),
                timeout=5,
            )
            self.notice = "Copied."
        elif name == "search":
            query = argument or await self.read("Search transcript")
            if query:
                index = self.transcript.text.casefold().find(query.casefold())
                if index >= 0:
                    self.transcript.buffer.cursor_position = index
                    self.app.layout.focus(self.transcript)
                else:
                    self.notice = "No matching transcript text."
        else:
            from vikram.terminal_commands import dispatch

            await dispatch(self, name, argument)
        if not self.full_screen and self.idle.is_set() and self.app.is_running:
            await self.print_regular()

    async def shell(self, command, *, include=True):
        if not self.idle.is_set():
            raise ValueError("Stop the active reply before running commands.")
        coding = getattr(self.session, "coding", None)
        if coding is None:
            raise ValueError("Choose and trust a workspace before running commands.")
        self.idle.clear()
        self.started = time.monotonic()
        item = {"role": "assistant", "text": "", "status": "running", "phase": "tool"}
        self.messages.append(item)
        original_output = coding.on_output
        partial = ""

        async def output(text):
            nonlocal partial
            partial = (partial + text)[-256000:]
            item["text"] = "```text\n" + terminal_text(partial) + "\n```"
            self.request_redraw()

        async def execute():
            coding.on_output = output
            try:
                result = await coding.dispatch(
                    "bash",
                    {"command": command},
                    interaction=self,
                    mode=self.session.mode,
                    user_initiated=True,
                )
                text = result.output or result.error or "(no output)"
                item.update(
                    text="```text\n" + terminal_text(text) + "\n```",
                    status="complete" if result.ok else "failed",
                )
                if include:
                    self.session.record_shell(command, text)
            except asyncio.CancelledError:
                item["status"] = "stopped"
                self.notice = "Command stopped. Partial output is excluded from context; completed file changes remain."
            except Exception:
                item["status"] = "failed"
                self.notice = "The command could not complete."
            finally:
                coding.on_output = original_output
                if self.dialog_future is not None and self.dialog_is_interaction:
                    self._finish_dialog(None)
                self.idle.set()
                self.redraw()
                if not self.full_screen and self.app.is_running:
                    await self.print_regular()

        self.task = asyncio.create_task(execute())
        await self.task

    async def external_editor(self):
        command = (
            (self.settings.get("editor") if self.settings else None)
            or os.environ.get("VISUAL")
            or os.environ.get("EDITOR")
        )
        if not command:
            self.notice = "Set VISUAL or EDITOR to use an external editor."
            return
        original = self.editor.text
        with tempfile.TemporaryDirectory(prefix="vikram-editor-") as folder:
            path = Path(folder) / "prompt.md"
            path.write_text(original)
            os.chmod(path, 0o600)
            from vikram.coding import child_environment

            result = await run_in_terminal(
                lambda: subprocess.run(
                    [*shlex.split(command), str(path)], env=child_environment()
                ),
                in_executor=True,
            )
            if result.returncode:
                self.notice = (
                    f"Editor exited with code {result.returncode}; prompt preserved."
                )
            else:
                self.editor.text = path.read_text()[:1_000_000]

    async def run(self):
        self.app.ttimeoutlen = 0.1
        self.app.timeoutlen = 0.3

        async def initialize():
            await self.ready.wait()
            if not self.full_screen:
                await self.print_regular()
            if hasattr(self.session, "catalog"):
                try:
                    await self.refresh_models()
                    if not any(
                        ModelRef.parse(m["name"]) == ModelRef.parse(self.session.model)
                        and m["available"]
                        for m in self.models
                    ):
                        self.notice = "Choose an installed model. Your selection is remembered for future conversations."
                        if self.dialog is None and self.idle.is_set():
                            await self.pick_model(models=self.models)
                except ValueError as error:
                    self.notice = str(error)
                    if self.dialog is None and self.idle.is_set():
                        await self.pick_model(models=[])
                self.app.invalidate()
            if getattr(self.session, "coding", None) is not None:
                from vikram.terminal_commands import extension_catalog

                try:
                    await extension_catalog(self)
                except Exception:
                    self.notice = "Some extension commands are unavailable. Inspect /extensions to reconnect."

        startup = asyncio.create_task(initialize())
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r"`httpx\.AsyncClient` support for OpenAI-compatible providers is deprecated",
                    category=PydanticAIDeprecationWarning,
                    module=r"vikram\.agent",
                )
                await self.app.run_async(pre_run=self.ready.set)
        finally:
            startup.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await startup
            await self.stop()
            self.save_draft()
            await self.session.close()

    async def print_regular(self):
        async with self._print_lock:
            end = len(self.messages)
            pending = self.messages[self.printed : end]
            if not pending:
                return

            def output():
                console = Console()
                for item in pending:
                    if item["role"] == "user":
                        console.print(
                            Text("You › " + terminal_text(item["text"]), style="bold")
                        )
                    else:
                        console.print(
                            render_reply(
                                item,
                                status=item.get("status", "complete"),
                                show_thinking=self.show_thinking,
                            )
                        )

            await run_in_terminal(output, in_executor=True)
            self.printed = end
            self.redraw()


def run_tui(session, *, settings=None, full_screen=True):
    keyboard = create_input()
    try:
        asyncio.run(
            TerminalApp(
                session, settings=settings, input=keyboard, full_screen=full_screen
            ).run()
        )
    finally:
        keyboard.close()
