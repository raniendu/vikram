"""Messages queued in other chats while one reply holds the single reply slot."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from test_desktop_runtime import ENTRY, SHOW
from test_desktop_store import png

from vikram.desktop.models import parse_model
from vikram.desktop.runtime import Runtime
from vikram.desktop.store import Store


class CrossChatQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name))
        self.a, self.b, self.c = (
            self.store.create_chat("local:1")["id"] for _ in range(3)
        )
        self.events, self.started = [], []
        # The first reply waits for `release` (or fails when `failure` is set).
        self.release, self.entered = asyncio.Event(), asyncio.Event()
        self.failure: Exception | None = None

        async def stream(model, thinking, history, prompt, **kwargs):
            self.started.append((kwargs["session_id"], prompt))
            if len(self.started) == 1:
                self.entered.set()
                yield {"text": "partial", "phase": "answering"}
                await self.release.wait()
                if self.failure is not None:
                    raise self.failure
            yield {"text": "answer", "phase": "answering"}

        self.runtime = Runtime(self.store, self.events.append, streamer=stream)
        model = {**parse_model(ENTRY, SHOW), "context_length": 100_000}
        self.runtime.catalog.refresh = AsyncMock(return_value=[model])
        self.runtime.models = [model]

    async def asyncTearDown(self):
        await self.runtime.close()
        self.store.close()
        self.temporary.cleanup()

    def draft(self, chat_id, text):
        self.store.save_draft(
            chat_id, text, self.store.chat(chat_id)["draft_revision"] + 1
        )

    async def start_first(self, chat_id, text="First"):
        self.draft(chat_id, text)
        await self.runtime.command("send", {"chat_id": chat_id}, "first")
        await asyncio.wait_for(self.entered.wait(), 1)

    async def queue_from_composer(self, chat_id, text):
        self.draft(chat_id, text)
        return await self.runtime.command(
            "send", {"chat_id": chat_id, "queue_if_busy": True}, f"q-{text}"
        )

    async def settled(self):
        for _ in range(400):
            await asyncio.sleep(0.005)
            task = self.runtime.queued_task
            if (
                not self.runtime.active
                and not self.runtime.starting
                and (task is None or task.done())
            ):
                return
        self.fail("Runtime queue did not settle")

    def queue(self, chat_id):
        return self.runtime.workbench.context(chat_id).queued_inputs

    def texts(self, chat_id):
        return [m["text"] for m in self.store.chat(chat_id)["messages"]]

    def sent_prompts(self):
        return [(chat, str(prompt)) for chat, prompt in self.started]

    async def test_send_while_busy_queues_draft_as_followup_for_that_chat(self):
        await self.start_first(self.a)
        result = await self.queue_from_composer(self.b, "Later in B")
        self.assertEqual(result["queued"]["text"], "Later in B")
        self.assertEqual(result["queued"]["kind"], "followup")
        self.assertEqual(result["active"]["chat_id"], self.a)
        self.assertEqual(result["chat"]["draft"], "")
        self.assertEqual(self.store.chat(self.b)["draft"], "")
        self.assertEqual([q["text"] for q in self.queue(self.b)], ["Later in B"])
        self.assertTrue(
            any(
                e["event"] == "workbench" and e.get("chat_id") == self.b
                for e in self.events
            )
        )
        # The one-reply rule is unchanged for a plain send.
        self.draft(self.c, "Not queued")
        with self.assertRaisesRegex(ValueError, "Another reply"):
            await self.runtime.command("send", {"chat_id": self.c}, "plain")
        self.assertEqual(self.store.chat(self.c)["draft"], "Not queued")
        self.assertEqual(self.queue(self.c), [])

    async def test_draft_with_attachments_is_not_queued(self):
        await self.start_first(self.a)
        self.draft(self.b, "With a picture")
        self.store.import_image(self.b, "draft.png", png())
        with self.assertRaisesRegex(ValueError, "Attachments"):
            await self.runtime.command(
                "send", {"chat_id": self.b, "queue_if_busy": True}, "q"
            )
        self.assertEqual(self.store.chat(self.b)["draft"], "With a picture")
        self.assertEqual(self.queue(self.b), [])

    async def test_queued_messages_start_oldest_first_across_chats(self):
        await self.start_first(self.a)
        await self.queue_from_composer(self.b, "b1")
        await self.runtime.workbench.command(
            {
                "action": "queue",
                "chat_id": self.a,
                "value": "a1",
                "options": {"kind": "followup"},
            }
        )
        await self.queue_from_composer(self.c, "c1")
        await self.queue_from_composer(self.b, "b2")
        self.release.set()
        await self.settled()
        self.assertEqual(
            [chat for chat, _ in self.started], [self.a, self.b, self.a, self.c, self.b]
        )
        self.assertEqual(self.texts(self.a), ["First", "a1"])
        self.assertEqual(self.texts(self.b), ["b1", "b2"])
        self.assertEqual(self.texts(self.c), ["c1"])
        for chat_id in (self.a, self.b, self.c):
            self.assertEqual(self.queue(chat_id), [])

    async def test_stop_pauses_own_queue_and_starts_other_chats(self):
        await self.start_first(self.a)
        await self.runtime.workbench.command(
            {
                "action": "queue",
                "chat_id": self.a,
                "value": "a1",
                "options": {"kind": "followup"},
            }
        )
        await self.queue_from_composer(self.b, "b1")
        await self.runtime.command("stop", {}, "stop")
        await self.settled()
        self.assertEqual(self.texts(self.b), ["b1"])
        self.assertEqual(self.texts(self.a), ["First"])
        paused = self.queue(self.a)
        self.assertEqual([(q["text"], q["held"]) for q in paused], [("a1", True)])
        # Resuming hands the paused message the free slot.
        await self.runtime.workbench.command(
            {"action": "queue_resume", "chat_id": self.a, "value": paused[0]["id"]}
        )
        await self.settled()
        self.assertEqual(self.texts(self.a), ["First", "a1"])
        self.assertEqual(self.queue(self.a), [])

    async def test_failed_reply_still_starts_other_chats(self):
        await self.start_first(self.a)
        await self.queue_from_composer(self.b, "b1")
        self.failure = RuntimeError("model went away")
        self.release.set()
        await self.settled()
        statuses = [
            a["status"] for a in self.store.chat(self.a)["messages"][0]["attempts"]
        ]
        self.assertEqual(statuses, ["failed"])
        self.assertEqual(self.texts(self.b), ["b1"])
        self.assertEqual(self.queue(self.b), [])

    async def test_completed_reply_releases_own_paused_queue(self):
        await self.start_first(self.a)
        context = self.runtime.workbench.context(self.a)
        context.queue_input("paused earlier", kind="followup")
        context.hold_queued()
        self.release.set()
        await self.settled()
        self.assertEqual(self.texts(self.a), ["First", "paused earlier"])

    async def test_message_that_cannot_start_is_paused_and_next_one_starts(self):
        await self.start_first(self.a)
        await self.queue_from_composer(self.b, "b1")
        await self.queue_from_composer(self.c, "c1")
        other = Store(self.store.root)
        try:
            # Another Vikram instance (the CLI) owns chat B.
            other.claim_chat(self.b)
            self.release.set()
            await self.settled()
        finally:
            other.close()
        self.assertEqual(self.texts(self.c), ["c1"])
        self.assertEqual(self.texts(self.b), [])
        self.assertEqual(
            [(q["text"], q["held"]) for q in self.queue(self.b)], [("b1", True)]
        )
        notices = [e["message"] for e in self.events if e["event"] == "notice"]
        self.assertTrue(any("paused" in notice for notice in notices))

    async def test_queue_while_idle_starts_right_away(self):
        await self.runtime.workbench.command(
            {
                "action": "queue",
                "chat_id": self.b,
                "value": "now",
                "options": {"kind": "followup"},
            }
        )
        await asyncio.wait_for(self.entered.wait(), 1)
        self.release.set()
        await self.settled()
        self.assertEqual(self.texts(self.b), ["now"])

    async def test_waiting_reply_holds_the_queue(self):
        from pydantic_ai_harness.ask_user import AskUserRequest, Question

        asked = asyncio.Event()

        async def asking(model, thinking, history, prompt, **kwargs):
            self.started.append((kwargs["session_id"], prompt))
            if len(self.started) == 1:
                asked.set()
                await kwargs["interaction"].ask(
                    AskUserRequest(
                        questions=(
                            Question(
                                header="Pick",
                                question="Which one?",
                                options=({"label": "x"}, {"label": "y"}),
                            ),
                        )
                    )
                )
            yield {"text": "answer", "phase": "answering"}

        self.runtime.streamer = asking
        self.draft(self.a, "Ask me")
        await self.runtime.command("send", {"chat_id": self.a}, "first")
        await asyncio.wait_for(asked.wait(), 1)
        await self.queue_from_composer(self.b, "b1")
        await asyncio.sleep(0.05)
        self.assertEqual(self.texts(self.b), [])
        self.assertEqual(len(self.queue(self.b)), 1)
        await self.runtime.command("stop", {}, "stop")
        await self.settled()
        self.assertEqual(self.texts(self.b), ["b1"])

    async def test_deleting_a_chat_drops_its_queue(self):
        await self.start_first(self.a)
        await self.queue_from_composer(self.b, "b1")
        await self.runtime.command("delete_chat", {"chat_id": self.b}, "delete")
        self.release.set()
        await self.settled()
        self.assertEqual([chat for chat, _ in self.started], [self.a])
        status = await self.runtime.workbench.command(
            {"action": "status", "chat_id": self.a}
        )
        self.assertEqual(status["queue_total"], 0)


if __name__ == "__main__":
    unittest.main()
