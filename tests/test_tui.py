import asyncio
import tempfile
import unittest
import warnings
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch

from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from pydantic_ai import PydanticAIDeprecationWarning

from vikram.desktop.store import Store
from vikram.session import Session
from vikram.settings import Settings
from vikram.tui import TerminalApp


class FakeCatalog:
    def __init__(self):
        self.broken = False

    async def refresh(self, **kwargs):
        if self.broken:
            raise ValueError("Model discovery is offline.")
        return [
            {
                "name": name,
                "available": True,
                "thinking": [False, True],
                "vision": False,
            }
            for name in ("local:1", "other:2")
        ]


class FakeSession:
    def __init__(self):
        self.model = "local:1"
        self.thinking = "on"
        self.mode = "normal"
        self.turns = []
        self.store = None
        self.chat_id = None
        self.workspace = Path.cwd()
        self._queue = []
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.catalog = FakeCatalog()
        self.coding = None
        self.attachments = []
        self.closed = False

    queued_inputs = Session.queued_inputs
    queue_input = Session.queue_input
    edit_queued = Session.edit_queued
    remove_queued = Session.remove_queued
    drain_queued = Session.drain_queued
    restore_queued = Session.restore_queued
    take_followup = Session.take_followup
    attach_file = Session.attach_file

    @property
    def session_id(self):
        return self.chat_id

    async def reply(self, text, *, interaction=None):
        self.started.set()
        yield {"text": "First", "phase": "answering"}
        await self.finish.wait()
        self.turns.append({"prompt": [text], "answer": "Final"})
        yield {"text": "Final", "phase": "answering"}

    async def close(self):
        self.closed = True
        if self.coding is not None:
            from vikram.integrations import close

            await close(self.coding)


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


@asynccontextmanager
async def running_terminal(session=None, **kwargs):
    with create_pipe_input() as keyboard:
        terminal = TerminalApp(
            session or FakeSession(), input=keyboard, output=DummyOutput(), **kwargs
        )
        task = asyncio.create_task(terminal.run())
        try:
            await terminal.ready.wait()
            yield terminal, keyboard
        finally:
            await terminal.stop()
            if not task.done() and not terminal.app.is_done:
                terminal.app.exit()
            await asyncio.wait_for(task, 3)


class TerminalAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_known_provider_deprecation_is_suppressed_during_reply(self):
        class WarningSession(FakeSession):
            async def reply(self, prompt, *, interaction=None):
                warnings.warn_explicit(
                    "`httpx.AsyncClient` support for OpenAI-compatible providers is deprecated",
                    PydanticAIDeprecationWarning,
                    "vikram/agent.py",
                    1,
                    module="vikram.agent",
                )
                warnings.warn("Unrelated warning", UserWarning)
                yield {"text": "Answer"}

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            async with running_terminal(WarningSession()) as (terminal, keyboard):
                keyboard.send_text("hello\r")
                await until(lambda: terminal.messages and terminal.idle.is_set())
            self.assertEqual(
                [str(item.message) for item in caught], ["Unrelated warning"]
            )

    async def test_draft_writes_are_debounced_and_oversized_text_stays_editable(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            session = Session("local:1", store=store)
            session.catalog = FakeCatalog()
            try:
                with patch.object(store, "save_draft", wraps=store.save_draft) as save:
                    async with running_terminal(session) as (terminal, keyboard):
                        for text in ("First", "Second", "Final draft"):
                            terminal.editor.text = text
                        self.assertEqual(save.call_count, 0)
                        await until(
                            lambda: store.chat(session.chat_id)["draft"]
                            == "Final draft"
                        )
                        self.assertEqual(save.call_count, 1)
                        terminal.editor.text = "x" * 1_000_001
                        terminal.save_draft()
                        self.assertEqual(
                            store.chat(session.chat_id)["draft"], "Final draft"
                        )
                        self.assertEqual(len(terminal.editor.text), 1_000_001)
                        self.assertIn("save limit", terminal.notice)
                        terminal.editor.text = "Final draft"
            finally:
                store.close()

    async def test_approval_shows_scrollable_inert_arguments_and_denies_by_default(
        self,
    ):
        async with running_terminal() as (terminal, keyboard):
            request = SimpleNamespace(
                summary="Run external tool",
                reason="Unknown effects",
                details='{"path": "/important/file", "content": "'
                + "long value " * 100
                + '"}\x1b]52;c;hidden\x07',
            )
            task = asyncio.create_task(terminal.approve(request))
            try:
                await until(lambda: terminal.dialog is not None)
                preview = terminal.dialog_preview
                self.assertIsNotNone(preview)
                self.assertTrue(preview.buffer.read_only())
                self.assertIn("/important/file", preview.text)
                self.assertNotIn("hidden", preview.text)
                await until(
                    lambda: preview.window in terminal.app.layout.visible_windows
                )
                keyboard.send_bytes(b"\t")
                await until(lambda: terminal.app.layout.has_focus(preview))
                keyboard.send_text("cannot modify")
                await asyncio.sleep(0)
                self.assertNotIn("cannot modify", preview.text)
                keyboard.send_text("\r")
                self.assertFalse(await asyncio.wait_for(task, 2))
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_newer_draft_survives_begin_and_stop_before_first_stream_event(self):
        from test_sessions import MODEL

        started, release, stream_started, finish = (asyncio.Event() for _ in range(4))

        async def refresh():
            started.set()
            await release.wait()
            return [MODEL]

        async def stream(*args, **kwargs):
            stream_started.set()
            await finish.wait()
            yield {"text": "answer"}

        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            session = Session("local:1", store=store, streamer=stream)
            session.catalog = FakeCatalog()
            try:
                async with running_terminal(session) as (terminal, keyboard):
                    await until(lambda: bool(terminal.models))
                    session.catalog.refresh = refresh
                    keyboard.send_text("First prompt\r")
                    await started.wait()
                    keyboard.send_text("Newer draft")
                    await until(
                        lambda: store.chat(session.chat_id)["draft"]
                        == "First prompt\n\nNewer draft"
                    )
                    release.set()
                    await stream_started.wait()
                    await until(
                        lambda: store.chat(session.chat_id)["draft"] == "Newer draft"
                    )
                    await terminal.stop()
                    self.assertEqual(terminal.editor.text, "Newer draft")
                    self.assertEqual(session.queued_inputs, [])
                    saved = store.chat(session.chat_id)
                    self.assertEqual(saved["messages"][0]["text"], "First prompt")
                    self.assertEqual(
                        saved["messages"][0]["attempts"][0]["status"], "stopped"
                    )
            finally:
                store.close()

    async def test_saved_draft_restores_and_typed_edits_flush_on_quit(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            session = Session("local:1", store=store)
            session.catalog = FakeCatalog()
            store.save_draft(session.chat_id, "Saved desktop draft 雪", 1)
            try:
                async with running_terminal(session) as (terminal, keyboard):
                    self.assertEqual(terminal.editor.text, "Saved desktop draft 雪")
                    keyboard.send_text(" plus new edits")
                    await until(
                        lambda: terminal.editor.text.endswith(" plus new edits")
                    )
                    await terminal.command("quit", "")
                self.assertEqual(
                    store.chat(session.chat_id)["draft"],
                    "Saved desktop draft 雪 plus new edits",
                )
            finally:
                store.close()

    async def test_missing_model_restores_prompt_to_editable_saved_draft(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            session = Session("missing:1", store=store)
            session.catalog = FakeCatalog()
            try:
                async with running_terminal(session) as (terminal, keyboard):
                    await until(lambda: terminal.dialog is not None)
                    keyboard.send_bytes(b"\x1b")
                    await until(lambda: terminal.dialog is None)
                    keyboard.send_text("Keep this prompt\r")
                    await until(
                        lambda: "Choose an installed model" not in terminal.notice
                        and terminal.idle.is_set()
                        and bool(terminal.messages)
                    )
                    self.assertEqual(terminal.editor.text, "Keep this prompt")
                    self.assertEqual(
                        store.chat(session.chat_id)["draft"], "Keep this prompt"
                    )
                    self.assertEqual(store.chat(session.chat_id)["messages"], [])
            finally:
                store.close()

    async def test_preflight_failure_keeps_newer_edits_and_queues_original(self):
        class FailingSession(FakeSession):
            async def reply(self, prompt, *, interaction=None):
                self.started.set()
                await self.finish.wait()
                raise ValueError("Model is unavailable")
                yield

        session = FailingSession()
        async with running_terminal(session) as (terminal, keyboard):
            keyboard.send_text("Original unsent prompt\r")
            await session.started.wait()
            keyboard.send_text("Newer editable text")
            await until(lambda: terminal.editor.text == "Newer editable text")
            session.finish.set()
            await terminal.wait_idle()
            self.assertEqual(terminal.editor.text, "Newer editable text")
            self.assertEqual(session.queued_inputs[0]["text"], "Original unsent prompt")

    async def test_cancel_during_preflight_restores_prompt(self):
        class WaitingSession(FakeSession):
            async def reply(self, prompt, *, interaction=None):
                self.started.set()
                await self.finish.wait()
                yield {"text": "never reached"}

        session = WaitingSession()
        async with running_terminal(session) as (terminal, keyboard):
            keyboard.send_text("Unsent prompt\r")
            await session.started.wait()
            await terminal.stop()
            self.assertEqual(terminal.editor.text, "Unsent prompt")
            self.assertEqual(session.turns, [])

    async def test_regular_stop_prints_partial_reply_to_scrollback(self):
        with create_pipe_input() as keyboard:
            session = FakeSession()
            terminal = TerminalApp(
                session, input=keyboard, output=DummyOutput(), full_screen=False
            )
            with (
                patch.object(
                    type(terminal.app),
                    "is_running",
                    new_callable=PropertyMock,
                    return_value=True,
                ),
                patch("vikram.tui.run_in_terminal", AsyncMock()) as run,
            ):
                terminal.start_reply("hello")
                await session.started.wait()
                await terminal.stop()
            self.assertEqual(terminal.printed, len(terminal.messages))
            self.assertEqual(terminal.messages[-1]["status"], "stopped")
            run.assert_awaited_once()

    async def test_footer_labels_context_estimate_and_actual_usage(self):
        with create_pipe_input() as keyboard:
            session = FakeSession()
            session.context_state = lambda: {
                "estimated_tokens": 100,
                "context_limit": 4096,
                "last_usage": {
                    "input_tokens": 90,
                    "output_tokens": 12,
                    "cost_usd": "0.0012",
                    "cost_estimated": True,
                },
            }
            terminal = TerminalApp(session, input=keyboard, output=DummyOutput())
            footer = "".join(text for _, text in terminal._footer())
            self.assertIn("context ~100/4096", footer)
            self.assertIn("90 in / 12 out", footer)
            self.assertIn("~$0.0012", footer)

    async def test_copy_keeps_full_text_and_removes_provider_keys_from_child(self):
        with create_pipe_input() as keyboard:
            terminal = TerminalApp(FakeSession(), input=keyboard, output=DummyOutput())
            text = "Full text " * 10000
            terminal.messages = [{"role": "assistant", "text": text}]
            with (
                patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-only-secret"}),
                patch("vikram.tui.subprocess.run") as run,
            ):
                await terminal.command("copy", "")
            self.assertEqual(run.call_args.kwargs["input"], text.encode())
            self.assertNotIn("OPENROUTER_API_KEY", run.call_args.kwargs["env"])
            self.assertEqual(run.call_args.kwargs["timeout"], 5)

    async def test_editor_remains_available_and_busy_enter_queues_steering(self):
        session = FakeSession()
        async with running_terminal(session) as (terminal, keyboard):
            keyboard.send_text("hello\r")
            await asyncio.wait_for(session.started.wait(), 2)
            keyboard.send_text("change direction\r")
            await until(lambda: bool(session.queued_inputs))
            self.assertEqual(session.queued_inputs[0]["text"], "change direction")
            session.finish.set()
            await asyncio.wait_for(terminal.wait_idle(), 3)
            self.assertEqual(session.turns[0]["answer"], "Final")
            self.assertEqual(len(session.turns), 2)
            self.assertFalse(session.queued_inputs)
            self.assertTrue(terminal.task.done())
            self.assertIsNone(terminal.task.exception())

    async def test_stop_restores_queued_input_without_marking_partial_complete(self):
        with create_pipe_input() as keyboard:
            session = FakeSession()
            terminal = TerminalApp(session, input=keyboard, output=DummyOutput())
            terminal.start_reply("hello")
            await session.started.wait()
            session.queue_input("next", kind="followup")
            await terminal.stop()
            self.assertIn("next", terminal.editor.text)
            self.assertEqual(session.turns, [])
            self.assertEqual(terminal.messages[-1]["status"], "stopped")

    async def test_keyboard_model_picker_preserves_draft_and_never_submits(self):
        async with running_terminal() as (terminal, keyboard):
            terminal.editor.text = "A draft with Unicode 雪"
            keyboard.send_bytes(b"\x0c")
            await until(lambda: terminal.dialog is not None)
            keyboard.send_text("other\r")
            await until(lambda: terminal.session.model == "other:2")
            self.assertEqual(terminal.editor.text, "A draft with Unicode 雪")
            self.assertFalse(terminal.session.started.is_set())

    async def test_model_refresh_failure_preserves_draft_and_application(self):
        async with running_terminal() as (terminal, keyboard):
            await until(lambda: bool(terminal.models))
            terminal.editor.text = "Keep this prompt"
            terminal.session.catalog.broken = True
            keyboard.send_bytes(b"\x0c")
            await until(lambda: "offline" in terminal.notice)
            self.assertEqual(terminal.editor.text, "Keep this prompt")
            self.assertTrue(terminal.app.is_running)
            self.assertFalse(terminal.session.started.is_set())

    async def test_thinking_and_cycle_resolve_qualified_local_alias(self):
        with create_pipe_input() as keyboard:
            terminal = TerminalApp(FakeSession(), input=keyboard, output=DummyOutput())
            terminal.session.model = "ollama:local:1"
            await terminal.command("thinking", "off")
            self.assertEqual(terminal.session.thinking, "off")
            await terminal.cycle_model()
            self.assertEqual(terminal.session.model, "other:2")

    async def test_multi_select_questions_return_all_selected_options(self):
        from pydantic_ai_harness.ask_user import QuestionOption

        from vikram.interaction import AskUserRequest, Question

        with create_pipe_input() as keyboard:
            terminal = TerminalApp(FakeSession(), input=keyboard, output=DummyOutput())
            question = Question(
                header="Scope",
                question="Which files?",
                options=(QuestionOption(label="A"), QuestionOption(label="B")),
                multi_select=True,
            )
            with patch.object(
                terminal, "select", AsyncMock(side_effect=["1", "2", "done"])
            ):
                response = await terminal.ask(AskUserRequest(questions=(question,)))
            self.assertEqual(response.answers[0].selected, ("A", "B"))

    async def test_resources_are_commands_and_reload_applies_theme_and_keys(self):
        with tempfile.TemporaryDirectory() as folder, create_pipe_input() as keyboard:
            root = Path(folder)
            (root / "templates").mkdir()
            (root / "templates/review.md").write_text("Review $ARGUMENTS carefully.")
            (root / "skills/check").mkdir(parents=True)
            (root / "skills/check/SKILL.md").write_text(
                "---\nname: check\ndescription: Check a change\n---\nInspect the requested change."
            )
            (root / "themes").mkdir()
            (root / "themes/custom.json").write_text('{"header":"#123456"}')
            (root / "keybindings.json").write_text('{"c-y":"models"}')
            settings = Settings(root=root)
            settings.set("theme", "custom")
            session = FakeSession()
            session.finish.set()
            terminal = TerminalApp(
                session, settings=settings, input=keyboard, output=DummyOutput()
            )
            self.addAsyncCleanup(session.close)
            completion = [
                item.text
                for item in terminal.editor.completer.get_completions(
                    Document("/"), None
                )
            ]
            self.assertIn("/review", completion)
            self.assertIn("/skill:check", completion)
            await terminal.submit("/review the parser")
            await terminal.wait_idle()
            self.assertEqual(
                session.turns[0]["prompt"], ["Review the parser carefully."]
            )
            await terminal.submit("/skill:check the parser")
            await terminal.wait_idle()
            self.assertIn(
                "Inspect the requested change.", session.turns[1]["prompt"][0]
            )
            await terminal.command("reload", "")
            self.assertEqual(
                terminal.app.style.get_attrs_for_style_str("class:header").color,
                "123456",
            )
            from prompt_toolkit.keys import Keys

            self.assertTrue(
                terminal.app.key_bindings.get_bindings_for_keys((Keys.ControlY,))
            )

    async def test_regular_scrollback_prints_once_and_does_not_skip_new_messages(self):
        with create_pipe_input() as keyboard:
            terminal = TerminalApp(
                FakeSession(), input=keyboard, output=DummyOutput(), full_screen=False
            )
            terminal.messages.append(
                {"role": "assistant", "text": "One", "status": "complete"}
            )

            async def print_then_append(callback, **kwargs):
                callback()
                terminal.messages.append(
                    {"role": "assistant", "text": "Two", "status": "complete"}
                )

            with (
                patch("vikram.tui.run_in_terminal", side_effect=print_then_append),
                patch("vikram.tui.Console"),
            ):
                await terminal.print_regular()
            self.assertEqual(terminal.printed, 1)
            terminal._load_transcript()
            self.assertEqual(terminal.printed, 0)

    async def test_finishing_reply_keeps_user_opened_dialog(self):
        async with running_terminal() as (terminal, keyboard):
            terminal.start_reply("Hello")
            await terminal.session.started.wait()
            dialog = asyncio.create_task(
                terminal.select("Queued input", [("one", "One")])
            )
            try:
                await until(lambda: terminal.dialog is not None)
                terminal.session.finish.set()
                await terminal.wait_idle()
                self.assertFalse(dialog.done())
                keyboard.send_text("\r")
                self.assertEqual(await dialog, "one")
            finally:
                dialog.cancel()
                await asyncio.gather(dialog, return_exceptions=True)

    async def test_long_output_resize_keeps_editor_responsive_and_full_copy(self):
        from prompt_toolkit.data_structures import Size

        class LongSession(FakeSession):
            async def reply(self, text, *, interaction=None):
                self.started.set()
                yield {"text": "雪" * 400_000, "phase": "answering"}
                await self.finish.wait()

        async with running_terminal(LongSession()) as (terminal, keyboard):
            terminal.start_reply("Long answer")
            await terminal.session.started.wait()
            with patch.object(
                terminal.app.output, "get_size", return_value=Size(rows=18, columns=42)
            ):
                terminal.redraw()
                keyboard.send_text("editable")
                await until(lambda: terminal.editor.text == "editable")
            self.assertLess(len(terminal.transcript.text), 35000)
            self.assertEqual(len(terminal.messages[-1]["text"]), 400_000)

    async def test_stop_cancels_user_shell_and_keeps_partial_output(self):
        from types import SimpleNamespace

        started, cancelled = asyncio.Event(), asyncio.Event()

        class Shell:
            on_output = None

            async def dispatch(self, *args, **kwargs):
                started.set()
                if self.on_output:
                    await self.on_output("Partial shell output")
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        with create_pipe_input() as keyboard:
            terminal = TerminalApp(FakeSession(), input=keyboard, output=DummyOutput())
            terminal.session.coding = Shell()
            task = asyncio.create_task(terminal.shell("long command"))
            try:
                await started.wait()
                await terminal.stop()
                self.assertTrue(cancelled.is_set())
                self.assertIn("Partial shell output", terminal.messages[-1]["text"])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_path_completion_quotes_spaces_and_keeps_at_marker(self):
        with tempfile.TemporaryDirectory() as folder, create_pipe_input() as keyboard:
            path = Path(folder) / "report with spaces.txt"
            path.write_text("hello")
            terminal = TerminalApp(FakeSession(), input=keyboard, output=DummyOutput())
            document = Document("@" + str(Path(folder) / "report"))
            options = list(terminal.editor.completer.get_completions(document, None))
            self.assertTrue(options)
            import shlex

            completed = (
                document.text[: document.cursor_position + options[0].start_position]
                + options[0].text
            )
            self.assertEqual(shlex.split(completed), ["@" + str(path)])

    async def test_unknown_command_is_not_sent_to_model(self):
        with create_pipe_input() as keyboard:
            session = FakeSession()
            terminal = TerminalApp(session, input=keyboard, output=DummyOutput())
            await terminal.submit("/does-not-exist")
            self.assertFalse(session.started.is_set())
            self.assertIn("Unknown command", terminal.notice)

    async def test_pasted_terminal_controls_never_reach_rendered_transcript(self):
        with create_pipe_input() as keyboard:
            terminal = TerminalApp(FakeSession(), input=keyboard, output=DummyOutput())
            terminal.messages.append(
                {"role": "user", "text": "safe\x1b]52;c;secret\x07"}
            )
            rendered = terminal.transcript_text()
            self.assertIn("safe", rendered)
            self.assertNotIn("secret", rendered)
            self.assertNotIn("\x1b", rendered)


class StartupModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_startup_model_opens_picker_and_saves_machine_default(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(root=Path(folder))
            session = FakeSession()
            session.model = "not-installed:1"
            async with running_terminal(session, settings=settings) as (
                terminal,
                keyboard,
            ):
                await until(lambda: bool(terminal.models))
                self.assertIsNotNone(
                    terminal.dialog, "A missing model must open its selector"
                )
                terminal.editor.text = "Keep my draft"
                keyboard.send_text("other\r")
                await until(lambda: terminal.dialog is None)
                self.assertEqual(session.model, "other:2")
                self.assertEqual(Settings(root=Path(folder)).get("model"), "other:2")
                self.assertEqual(terminal.editor.text, "Keep my draft")
                self.assertFalse(session.started.is_set())

    async def test_explicit_model_change_persists_across_new_cli_process(self):
        import os

        from vikram import cli

        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(root=Path(folder))
            async with running_terminal(settings=settings) as (terminal, keyboard):
                await terminal.change_model("other:2")
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("vikram.cli._chat") as chat,
            ):
                cli.main(["--session-dir", folder, "Hello"])
            self.assertEqual(chat.call_args.args[0], "other:2")


class DefaultModelControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_can_choose_default_without_changing_current_conversation(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(root=Path(folder))
            async with running_terminal(settings=settings) as (terminal, keyboard):
                keyboard.send_text("/settings\r")
                await until(lambda: terminal.dialog is not None)
                keyboard.send_text("Default model\r")
                await until(lambda: "Default model ·" in str(terminal.dialog.title))
                keyboard.send_text("other\r")
                await until(lambda: terminal.dialog is None)
                self.assertEqual(settings.get("model"), "other:2")
                self.assertEqual(terminal.session.model, "local:1")

    async def test_picker_refresh_recovers_without_losing_draft(self):
        session = FakeSession()
        session.model = "missing:1"
        session.catalog.broken = True
        async with running_terminal(session) as (terminal, keyboard):
            await until(lambda: "offline" in terminal.notice)
            await until(lambda: terminal.dialog is not None)
            terminal.editor.text = "Keep this draft"
            session.catalog.broken = False
            previous = terminal.dialog
            keyboard.send_text("Refresh\r")
            await until(
                lambda: terminal.dialog is not None
                and terminal.dialog is not previous
                and bool(terminal.models)
            )
            keyboard.send_text("other\r")
            await until(lambda: terminal.dialog is None)
            self.assertEqual(session.model, "other:2")
            self.assertEqual(terminal.editor.text, "Keep this draft")
