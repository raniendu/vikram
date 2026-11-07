import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from test_desktop_store import png
from test_tui import FakeSession

from vikram.desktop.store import Store
from vikram.session import Session
from vikram.settings import Settings
from vikram.terminal_commands import (
    dispatch,
    extension_catalog,
    extension_command,
    resource_prompt,
    switch_session,
)
from vikram.tui import TerminalApp


class TerminalCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_template_arguments_preserve_natural_apostrophes(self):
        with tempfile.TemporaryDirectory() as folder, create_pipe_input() as keyboard:
            root = Path(folder)
            (root / "templates").mkdir()
            (root / "templates/review.md").write_text("Review $ARGUMENTS")
            terminal = TerminalApp(
                FakeSession(),
                settings=Settings(root=root),
                input=keyboard,
                output=DummyOutput(),
            )
            self.assertEqual(
                resource_prompt(terminal, "review", "what's changed"),
                "Review what's changed",
            )

    async def test_extension_command_discovers_schema_and_renders_declarations(self):
        with create_pipe_input() as keyboard:
            session = FakeSession()
            manager = SimpleNamespace(
                list_commands=AsyncMock(
                    return_value=[
                        {
                            "name": "ext__demo__greet",
                            "description": "Greeting",
                            "input_schema": {
                                "properties": {"name": {"type": "string"}}
                            },
                        }
                    ]
                )
            )
            session.coding = SimpleNamespace(extensions=manager)
            terminal = TerminalApp(session, input=keyboard, output=DummyOutput())
            with (
                patch("vikram.integrations.start", AsyncMock()) as start,
                patch(
                    "vikram.integrations.run_command",
                    AsyncMock(
                        return_value={
                            "ok": True,
                            "output": "Hello",
                            "ui": [
                                {"type": "notice", "text": "Ready"},
                                {
                                    "type": "table",
                                    "columns": ["Name"],
                                    "rows": [["Ada"]],
                                },
                                {
                                    "type": "progress",
                                    "label": "Work",
                                    "value": 1,
                                    "total": 2,
                                },
                            ],
                        }
                    ),
                ) as command,
            ):
                await extension_command(terminal, "ext__demo__greet", "Ada Lovelace")
                start.assert_awaited_once_with(session.coding)
                command.assert_awaited_once_with(
                    session.coding, "ext__demo__greet", {"name": "Ada Lovelace"}
                )
                self.assertEqual(session.coding.mode, "normal")
                self.assertIs(manager.interaction, terminal)
                self.assertIn("Work: 1/2", terminal.messages[-1]["text"])
                self.assertIn("Ada", terminal.messages[-1]["text"])
                session.mode = "plan"
                self.assertIsNone(await extension_catalog(terminal))
                self.assertEqual(terminal.extension_commands, {})
                self.assertEqual(start.await_count, 1)

    async def test_clipboard_attachment_and_removal_without_saved_session(self):
        with create_pipe_input() as keyboard:
            session = FakeSession()
            terminal = TerminalApp(session, input=keyboard, output=DummyOutput())
            with patch(
                "vikram.terminal_commands.read_clipboard_image", return_value=png()
            ):
                await terminal.command("paste-image", "")
            self.assertEqual(session.attachments[0]["name"], "Clipboard image.png")
            with patch.object(
                terminal, "select", AsyncMock(return_value=session.attachments[0]["id"])
            ):
                await terminal.command("attachments", "")
            self.assertEqual(session.attachments, [])

    async def test_failed_session_claim_preserves_current_resources_and_draft(self):
        with tempfile.TemporaryDirectory() as folder, create_pipe_input() as keyboard:
            root = Path(folder)
            store = Store(root / "state")
            current = Session("local:1", store=store, workspace=root)
            target = Session("local:1", store=store, workspace=root)
            coding = SimpleNamespace(close=AsyncMock(), mcp=None, extensions=None)
            current.coding = coding
            terminal = TerminalApp(
                current,
                settings=Settings(root=root / "settings"),
                input=keyboard,
                output=DummyOutput(),
            )
            terminal.editor.text = "Keep my draft"
            try:
                with self.assertRaises(ValueError):
                    await switch_session(terminal, target.chat_id)
                self.assertIs(terminal.session, current)
                self.assertIs(current.coding, coding)
                coding.close.assert_not_awaited()
                self.assertEqual(terminal.editor.text, "Keep my draft")
            finally:
                await current.close()
                await target.close()
                store.close()

    async def test_at_path_with_spaces_imports_file_and_preserves_prompt_spacing(self):
        with tempfile.TemporaryDirectory() as folder, create_pipe_input() as keyboard:
            root = Path(folder)
            path = root / "report with spaces.txt"
            path.write_text("A small document")
            store = Store(root / "state")
            try:
                session = FakeSession()
                session.store = store
                session.chat_id = store.create_chat("local:1")["id"]
                session.finish.set()
                terminal = TerminalApp(session, input=keyboard, output=DummyOutput())
                await terminal.submit(f'@"{path}" Explain  these\n  lines')
                await terminal.wait_idle()
                self.assertEqual(
                    session.turns[0]["prompt"], ["Explain  these\n  lines"]
                )
                self.assertEqual(
                    store.chat(session.chat_id)["attachments"][0]["name"], path.name
                )
            finally:
                store.close()

    async def test_clipboard_image_has_descriptive_terminal_fallback(self):
        with tempfile.TemporaryDirectory() as folder, create_pipe_input() as keyboard:
            store = Store(Path(folder))
            try:
                session = FakeSession()
                session.store = store
                session.chat_id = store.create_chat("local:1")["id"]
                terminal = TerminalApp(session, input=keyboard, output=DummyOutput())
                terminal.models = await session.catalog.refresh()
                with patch(
                    "vikram.terminal_commands.read_clipboard_image", return_value=png()
                ):
                    await terminal.command("paste-image", "")
                attachments = store.chat(session.chat_id)["attachments"]
                self.assertEqual(attachments[0]["kind"], "image")
                self.assertIn("preview", terminal.notice.lower())
                self.assertIn("text-only", terminal.notice.lower())
                self.assertFalse(session.started.is_set())
            finally:
                store.close()

    async def test_switch_closes_integrations_preserves_drafts_and_resets_scrollback(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder, create_pipe_input() as keyboard:
            root = Path(folder)
            store = Store(root / "state")
            settings = Settings(root=root / "state", workspace=root)
            old = Session("local:1", store=store, workspace=root)
            coding = SimpleNamespace(
                close=AsyncMock(),
                mcp=SimpleNamespace(close=AsyncMock()),
                extensions=SimpleNamespace(close=AsyncMock()),
            )
            old.coding = coding
            target = store.create_chat("other:2", workspace=root)
            store.save_draft(target["id"], "Target draft", 1)
            terminal = TerminalApp(
                old,
                settings=settings,
                input=keyboard,
                output=DummyOutput(),
                full_screen=False,
            )
            terminal.printed = 6
            terminal.editor.text = "Old unsent draft"
            try:
                await switch_session(terminal, target["id"])
                self.assertEqual(terminal.editor.text, "Target draft")
                self.assertEqual(store.chat(old.chat_id)["draft"], "Old unsent draft")
                self.assertEqual(terminal.printed, 0)
                self.assertEqual(coding.close.await_count, 1)
                self.assertEqual(coding.mcp.close.await_count, 1)
                self.assertEqual(coding.extensions.close.await_count, 1)
            finally:
                await terminal.session.close()
                store.close()

    async def test_session_picker_delete_requires_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as folder, create_pipe_input() as keyboard:
            root = Path(folder)
            store = Store(root / "state")
            settings = Settings(root=root / "state", workspace=root)
            session = Session("local:1", store=store, workspace=root)
            target = store.create_chat("local:1", workspace=root)
            terminal = TerminalApp(
                session, settings=settings, input=keyboard, output=DummyOutput()
            )
            try:
                with patch.object(
                    terminal,
                    "select",
                    AsyncMock(side_effect=[target["id"], "delete", False]),
                ):
                    await dispatch(terminal, "resume", "")
                self.assertEqual(store.chat(target["id"])["id"], target["id"])
                with patch.object(
                    terminal,
                    "select",
                    AsyncMock(side_effect=[target["id"], "delete", True]),
                ):
                    await dispatch(terminal, "resume", "")
                self.assertNotIn(
                    target["id"], [item["id"] for item in store.list_chats()]
                )
            finally:
                await terminal.session.close()
                store.close()


if __name__ == "__main__":
    unittest.main()
