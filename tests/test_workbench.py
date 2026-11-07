import tempfile
import unittest
from pathlib import Path

from vikram.desktop.runtime import Runtime
from vikram.desktop.store import Store


class WorkbenchTests(unittest.IsolatedAsyncioTestCase):
    async def test_workspace_trust_and_branches_use_shared_library(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root / "state")
            runtime = Runtime(store, lambda event: None)
            chat = store.create_chat("local:1")
            cid = chat["id"]
            try:
                await runtime.command(
                    "workbench",
                    {"action": "workspace", "chat_id": cid, "value": str(root)},
                    "1",
                )
                status = await runtime.command(
                    "workbench", {"action": "status", "chat_id": cid}, "2"
                )
                self.assertFalse(status["trusted"])
                await runtime.command(
                    "workbench",
                    {"action": "trust", "chat_id": cid, "options": {"trusted": True}},
                    "3",
                )
                status = await runtime.command(
                    "workbench", {"action": "status", "chat_id": cid}, "4"
                )
                self.assertTrue(status["trusted"])
                clone = await runtime.command(
                    "workbench", {"action": "clone", "chat_id": cid}, "5"
                )
                self.assertNotEqual(clone["id"], cid)
                self.assertEqual(clone["workspace"], str(root.resolve()))
            finally:
                await runtime.close()
                store.close()

    async def test_noninteractive_approval_kind_is_validated(self):
        from vikram.desktop.protocol import validate_command

        for kind in ("coding", "mcp", "extension"):
            raw = (
                '{"v":1,"id":"r","method":"respond","params":{"chat_id":"'
                + "a" * 32
                + '","attempt_id":"'
                + "b" * 32
                + '","interaction_id":"tool","kind":"'
                + kind
                + '","decision":"dismiss"}}'
            )
            self.assertEqual(validate_command(raw).params["kind"], kind)


class ModelDefaultTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.addCleanup(self.store.close)
        self.runtime = Runtime(self.store, lambda event: None)
        self.addAsyncCleanup(self.runtime.close)
        self.runtime.models = [
            {"name": name, "available": True} for name in ("local:1", "other:2")
        ]

    async def test_app_default_applies_to_future_chats_without_changing_current_chat(
        self,
    ):
        from vikram.settings import Settings

        chat = self.store.create_chat("local:1")
        await self.runtime.command(
            "workbench", {"action": "default_model", "value": "other:2"}, "default"
        )
        status = await self.runtime.command("workbench", {"action": "status"}, "status")
        self.assertEqual(status["default_model"], "other:2")
        self.assertEqual(Settings(root=self.store.root).get("model"), "other:2")
        self.assertEqual(self.store.chat(chat["id"])["model"], "local:1")
        new_chat = await self.runtime.command("create_chat", {}, "new")
        self.assertEqual(new_chat["model"], "other:2")

    async def test_cli_default_is_shared_with_desktop_and_desktop_selection_updates_it(
        self,
    ):
        from vikram.settings import Settings

        Settings(root=self.store.root).set("model", "other:2")
        new_chat = await self.runtime.command("create_chat", {}, "new")
        self.assertEqual(new_chat["model"], "other:2")
        await self.runtime.command(
            "update_chat",
            {"chat_id": new_chat["id"], "fields": {"model": "local:1"}},
            "select",
        )
        self.assertEqual(Settings(root=self.store.root).get("model"), "local:1")
        self.assertEqual(
            (await self.runtime.command("create_chat", {}, "next"))["model"], "local:1"
        )

    async def test_missing_configured_default_does_not_silently_choose_another_model(
        self,
    ):
        from vikram.settings import Settings

        Settings(root=self.store.root).set("model", "not-installed:1")
        self.store.set_preference("last_model", "other:2")
        self.assertIsNone(self.runtime.suggested_model())
