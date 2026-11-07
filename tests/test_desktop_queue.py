import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_desktop_runtime import ENTRY, SHOW
from test_desktop_store import png

from vikram.desktop.models import parse_model
from vikram.desktop.runtime import Runtime
from vikram.desktop.store import Store


class DesktopQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name))
        self.chat = self.store.create_chat("local:1")
        self.cid = self.chat["id"]
        self.events, self.calls = [], []

        async def stream(model, thinking, history, prompt, **kwargs):
            self.calls.append({"model": model, "history": history, "prompt": prompt})
            yield {
                "text": "answer",
                "phase": "answering",
                "usage": {"input_tokens": 12, "output_tokens": 3},
            }

        self.runtime = Runtime(self.store, self.events.append, streamer=stream)
        self.model = {**parse_model(ENTRY, SHOW), "context_length": 1000}
        self.runtime.catalog.refresh = AsyncMock(return_value=[self.model])
        self.runtime.models = [self.model]

    async def asyncTearDown(self):
        await self.runtime.stop()
        await self.runtime.close()
        self.store.close()
        self.temporary.cleanup()

    def draft(self, text):
        self.store.save_draft(
            self.cid, text, self.store.chat(self.cid)["draft_revision"] + 1
        )

    def completed_turn(self, text="u" * 1000):
        self.draft(text)
        attempt = self.store.begin(
            self.cid,
            "local:1",
            None,
            f"prior-{len(self.store.chat(self.cid)['messages'])}",
        )
        self.store.checkpoint(attempt["id"], "a" * 1000, "local:1")
        self.store.finish(attempt["id"], "complete")

    async def settled(self):
        for _ in range(200):
            await asyncio.sleep(0.005)
            if (
                not self.runtime.active
                and not self.runtime.starting
                and (
                    not getattr(self.runtime, "queued_task", None)
                    or self.runtime.queued_task.done()
                )
            ):
                return
        self.fail("Runtime queue did not settle")

    async def test_auto_compaction_uses_alias_metadata_and_preserves_full_transcript(
        self,
    ):
        for _ in range(4):
            self.completed_turn()
        self.store.update_chat(self.cid, model="ollama:local:1")
        context = self.runtime.workbench.context(self.cid)
        context.summarizer = AsyncMock(return_value="Retained decisions and effects")
        self.draft("Continue")
        await self.runtime.send(self.cid, "new")
        await self.runtime.task
        saved = self.store.chat(self.cid)
        self.assertEqual(saved["context_summary"], "Retained decisions and effects")
        self.assertEqual(len(saved["messages"]), 5)
        self.assertIn("Retained decisions", str(self.calls[-1]["history"]))
        self.assertEqual(
            saved["messages"][-1]["attempts"][0]["usage"]["output_tokens"], 3
        )

    async def test_compaction_failure_preserves_history_and_continues(self):
        for _ in range(3):
            self.completed_turn()
        context = self.runtime.workbench.context(self.cid)
        context.summarizer = AsyncMock(
            side_effect=RuntimeError("private response detail")
        )
        self.draft("Continue")
        await self.runtime.send(self.cid, "new")
        await self.runtime.task
        self.assertEqual(self.store.chat(self.cid)["context_summary"], "")
        self.assertEqual(len(self.calls[-1]["history"]), 6)
        notices = [
            event["message"] for event in self.events if event["event"] == "notice"
        ]
        self.assertTrue(
            any("compaction failed" in notice.lower() for notice in notices)
        )
        self.assertNotIn("private response detail", str(notices))

    async def test_natural_completion_delivers_unsent_steering_then_followup_without_draft_loss(
        self,
    ):
        context = self.runtime.workbench.context(self.cid)
        context.queue_input("Steer after boundary", kind="steer")
        context.queue_input("Follow up", kind="followup")
        self.draft("First")
        await self.runtime.send(self.cid, "first")
        self.draft("An editable next draft")
        self.store.import_image(self.cid, "draft.png", png())
        revision = self.store.chat(self.cid)["draft_revision"]
        await self.settled()
        saved = self.store.chat(self.cid)
        self.assertEqual(
            [message["text"] for message in saved["messages"]],
            ["First", "Steer after boundary", "Follow up"],
        )
        self.assertEqual(saved["draft"], "An editable next draft")
        self.assertEqual(saved["draft_revision"], revision)
        self.assertIsNone(saved["attachments"][0]["message_id"])
        self.assertEqual(context.queued_inputs, [])

    async def test_failed_followup_send_keeps_queue_and_draft_editable(self):
        context = self.runtime.workbench.context(self.cid)
        item = context.queue_input("Queued")
        self.draft("First")
        await self.runtime.send(self.cid, "first")
        self.draft("Keep draft")
        self.runtime.catalog.refresh = AsyncMock(side_effect=ValueError("Unavailable"))
        await self.settled()
        self.assertEqual(context.queued_inputs[0]["id"], item["id"])
        self.assertEqual(self.store.chat(self.cid)["draft"], "Keep draft")
        self.assertEqual(len(self.store.chat(self.cid)["messages"]), 1)
        self.assertTrue(any(event["event"] == "notice" for event in self.events))

    async def test_cancel_preserves_queues_and_stops_pending_followup_metadata(self):
        context = self.runtime.workbench.context(self.cid)
        context.queue_input("Queued")
        self.draft("First")
        await self.runtime.send(self.cid, "first")
        started = asyncio.Event()

        async def blocked_metadata():
            started.set()
            await asyncio.Event().wait()

        self.runtime.catalog.refresh = blocked_metadata
        await asyncio.wait_for(started.wait(), 1)
        await self.runtime.stop()
        self.assertEqual(len(context.queued_inputs), 1)
        self.assertFalse(self.runtime.starting)
        self.assertIsNone(self.runtime.active)

    async def test_steering_persistence_failure_does_not_drop_queue(self):
        context = self.runtime.workbench.context(self.cid)
        context.queue_input("Preserve me")
        with patch.object(self.store, "append_steering", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.runtime.workbench.boundary(self.cid, "attempt")
        self.assertEqual(context.queued_inputs[0]["text"], "Preserve me")

    async def test_stopping_generation_preserves_unsent_queue_and_draft(self):
        entered = asyncio.Event()

        async def blocked(*args, **kwargs):
            entered.set()
            yield {"text": "partial", "phase": "answering"}
            await asyncio.Event().wait()

        self.runtime.streamer = blocked
        context = self.runtime.workbench.context(self.cid)
        context.queue_input("Unsent steering")
        context.queue_input("Unsent followup", kind="followup")
        self.draft("First")
        await self.runtime.send(self.cid, "first")
        await entered.wait()
        self.draft("Keep draft")
        await self.runtime.stop()
        self.assertEqual(len(context.queued_inputs), 2)
        self.assertEqual(self.store.chat(self.cid)["draft"], "Keep draft")
        self.assertEqual(len(self.store.chat(self.cid)["messages"]), 1)

    async def test_manual_compaction_owns_chat_and_does_not_overwrite_draft(self):
        self.completed_turn()
        self.draft("Keep editable draft")
        started, release = asyncio.Event(), asyncio.Event()

        async def summarize(turns, instructions):
            started.set()
            await release.wait()
            return "Summary"

        self.runtime.workbench.context(self.cid).summarizer = summarize
        task = asyncio.create_task(
            self.runtime.workbench.command({"action": "compact", "chat_id": self.cid})
        )
        await started.wait()
        other = Store(self.store.root)
        try:
            with self.assertRaises(ValueError):
                other.claim_chat(self.cid)
            with self.assertRaises(ValueError):
                await self.runtime.send(self.cid, "concurrent")
        finally:
            other.close()
            release.set()
            await task
        self.assertEqual(self.store.chat(self.cid)["draft"], "Keep editable draft")
        self.assertFalse(self.runtime.starting)

    async def test_extension_commands_return_results_without_changing_draft(self):
        self.draft("Keep draft")
        coding = self.runtime.workbench.coding(self.cid)
        coding.extensions = SimpleNamespace(
            mode="normal",
            list_commands=AsyncMock(return_value=[{"name": "ext__pkg__command"}]),
            close=AsyncMock(),
        )
        with (
            patch("vikram.integrations.start", AsyncMock()),
            patch(
                "vikram.integrations.run_command",
                AsyncMock(return_value={"ok": True, "output": "command result"}),
            ) as run,
        ):
            commands = await self.runtime.workbench.command(
                {"action": "extension_commands", "chat_id": self.cid}
            )
            result = await self.runtime.workbench.command(
                {
                    "action": "extension_command",
                    "chat_id": self.cid,
                    "value": commands[0]["name"],
                    "options": {"arguments": {"value": "explicit"}},
                }
            )
        self.assertTrue(result["ok"])
        self.assertEqual(self.store.chat(self.cid)["draft"], "Keep draft")
        self.assertEqual(
            run.await_args.args[1:], ("ext__pkg__command", {"value": "explicit"})
        )


if __name__ == "__main__":
    unittest.main()
