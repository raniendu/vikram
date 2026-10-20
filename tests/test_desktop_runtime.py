import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic_ai.messages import BinaryContent, ModelRequest, UserPromptPart
from test_desktop_store import png

from vikram.desktop.history import inference_messages
from vikram.desktop.models import ModelCatalog, parse_model
from vikram.desktop.runtime import Runtime
from vikram.desktop.store import Store

ENTRY = {
    "name": "local:1",
    "size": 1000,
    "digest": "abc",
    "details": {"format": "gguf"},
}
SHOW = {
    "capabilities": ["completion", "vision", "thinking"],
    "details": {"format": "gguf"},
    "thinking": {"values": [False, True], "default": True},
}


class MetadataTests(unittest.TestCase):
    def test_local_admission_and_explicit_thinking(self):
        m = parse_model(ENTRY, SHOW)
        self.assertTrue(m["available"])
        self.assertEqual(m["thinking"], [False, True])
        self.assertTrue(m["vision"])
        self.assertEqual(parse_model(ENTRY, {**SHOW, "thinking": None})["thinking"], [])

    def test_remote_alias_and_unknown_artifact_rejected(self):
        self.assertFalse(
            parse_model(ENTRY, {**SHOW, "remote_host": "https://ollama.com"})[
                "available"
            ]
        )
        self.assertFalse(
            parse_model({**ENTRY, "remote_model": "cloud"}, SHOW)["available"]
        )
        self.assertFalse(parse_model({**ENTRY, "size": 0}, SHOW)["available"])
        self.assertFalse(
            parse_model(ENTRY, {**SHOW, "capabilities": ["embedding"]})["available"]
        )
        self.assertFalse(parse_model(ENTRY, {})["available"])


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.chat = self.store.create_chat("local:1")
        self.events = []
        self.closed = asyncio.Event()
        self.release = asyncio.Event()
        self.started = asyncio.Event()

        async def fake_stream(model, thinking, history, prompt, **options):
            self.started.set()
            try:
                yield {
                    "text": "hello",
                    "actual_model": "reported:1",
                    "phase": "answering",
                    "reasoning": "Model reasoning",
                }
                await self.release.wait()
                yield {
                    "text": "hello world",
                    "actual_model": "reported:1",
                    "phase": "answering",
                }
            finally:
                self.closed.set()

        self.runtime = Runtime(self.store, self.events.append, streamer=fake_stream)

        async def discover():
            return [parse_model(ENTRY, SHOW)]

        self.runtime.catalog.refresh = discover

    async def asyncTearDown(self):
        await self.runtime.stop()
        self.store.close()
        self.tmp.cleanup()

    async def send(self):
        self.store.save_draft(self.chat["id"], "question", 1)
        await self.runtime.send(self.chat["id"], "r1")
        await self.started.wait()
        await asyncio.sleep(0)

    async def test_stop_immediately_before_stream_task_starts(self):
        self.store.save_draft(self.chat["id"], "question", 1)
        await self.runtime.send(self.chat["id"], "r1")
        await self.runtime.stop()
        self.assertIsNone(self.runtime.active)
        self.assertEqual(
            self.store.chat(self.chat["id"])["messages"][0]["attempts"][0]["status"],
            "stopped",
        )

    async def test_waiting_for_ollama_is_not_reported_as_thinking(self):
        await self.send()
        first = next(e for e in self.events if e["event"] == "attempt")
        self.assertEqual(first["phase"], "loading")

    async def test_immediate_stop_survives_persistence_failure(self):
        self.store.save_draft(self.chat["id"], "question", 1)
        await self.runtime.send(self.chat["id"], "r1")
        with patch.object(self.store, "finish", side_effect=OSError("disk full")):
            await self.runtime.stop()
        self.assertIsNone(self.runtime.active)
        self.assertTrue(
            any(
                e.get("unsaved") and e.get("status") == "interrupted"
                for e in self.events
            )
        )

    async def test_cancellation_ignores_generator_late_chunk(self):
        async def stubborn(model, thinking, history, prompt, **options):
            self.started.set()
            yield {"text": "saved", "phase": "answering"}
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                yield {"text": "late", "phase": "answering"}
            finally:
                self.closed.set()

        self.runtime.streamer = stubborn
        await self.send()
        await self.runtime.stop()
        self.assertTrue(self.closed.is_set())
        self.assertEqual(
            self.store.chat(self.chat["id"])["messages"][0]["attempts"][0]["content"],
            "saved",
        )

    async def test_stop_releases_slot_only_after_transport_closed(self):
        await self.send()
        with self.assertRaises(ValueError):
            await self.runtime.send(self.chat["id"], "r2")
        await self.runtime.stop()
        self.assertTrue(self.closed.is_set())
        self.assertIsNone(self.runtime.active)
        attempt = self.store.chat(self.chat["id"])["messages"][0]["attempts"][0]
        self.assertEqual(attempt["status"], "stopped")
        self.assertEqual(attempt["content"], "hello")
        self.assertEqual(attempt["reasoning"], "Model reasoning")
        self.assertTrue(
            all(
                e.get("chat_id") == self.chat["id"]
                for e in self.events
                if e["event"] == "attempt"
            )
        )

    async def test_completed_and_retry_capture_model_settings(self):
        await self.send()
        self.store.update_chat(self.chat["id"], model="next:1")
        self.release.set()
        await self.runtime.task
        attempt = self.store.chat(self.chat["id"])["messages"][0]["attempts"][0]
        self.assertEqual(attempt["requested_model"], "local:1")
        self.assertEqual(attempt["actual_model"], "reported:1")
        self.assertEqual(attempt["status"], "complete")

    async def test_disk_failure_emits_unsaved_output_and_stops(self):
        with patch.object(self.store, "checkpoint", side_effect=OSError("disk full")):
            await self.send()
            await self.runtime.task
        self.assertTrue(self.closed.is_set())
        self.assertTrue(
            any(
                e.get("unsaved")
                and e.get("content") == "hello"
                and e.get("reasoning") == "Model reasoning"
                for e in self.events
            )
        )

    async def test_unsaved_replacement_has_later_revision_than_saved_intermediate(self):
        checkpoint = self.store.checkpoint

        def fail_final(attempt_id, content, actual, **options):
            if content == "42":
                raise OSError("disk full")
            return checkpoint(attempt_id, content, actual, **options)

        async def replaced(*args, **options):
            yield {"text": "Checking the web now…", "phase": "searching"}
            yield {"text": "42", "phase": "answering"}

        self.runtime.streamer = replaced
        self.store.save_draft(self.chat["id"], "question", 1)
        with patch.object(self.store, "checkpoint", side_effect=fail_final):
            await self.runtime.send(self.chat["id"], "r1")
            await self.runtime.task
        saved = self.store.chat(self.chat["id"])["messages"][0]["attempts"][0]
        unsaved = next(e for e in self.events if e.get("unsaved"))
        self.assertEqual(saved["content"], "Checking the web now…")
        self.assertEqual(unsaved["content"], "42")
        self.assertGreater(unsaved["revision"], saved["revision"])

    async def test_history_omits_partial_and_applies_vision_policy(self):
        cid = self.chat["id"]
        self.store.import_image(cid, "photo.png", png())
        self.store.save_draft(cid, "look", 1)
        attempt = self.store.begin(cid, "local:1", None, "r0")
        self.store.checkpoint(attempt["id"], "failed fragment", None)
        self.store.finish(attempt["id"], "failed")
        self.store.save_draft(cid, "next", 3)
        current = self.store.begin(cid, "local:1", None, "r1")
        history, prompt = inference_messages(
            self.store, cid, current["message_id"], vision=False
        )
        self.assertEqual(len(history), 1)
        self.assertIn("image omitted", str(history[0].parts[0].content).lower())
        self.assertNotIn("failed fragment", str(history))
        history, prompt = inference_messages(
            self.store, cid, current["message_id"], vision=True
        )
        self.assertTrue(
            any(isinstance(p, BinaryContent) for p in history[0].parts[0].content)
        )

    async def test_text_model_rejects_draft_images_before_persisting_user(self):
        self.store.import_image(self.chat["id"], "photo.png", png())

        async def text_models():
            return [{**parse_model(ENTRY, SHOW), "vision": False}]

        self.runtime.catalog.refresh = text_models
        with self.assertRaises(ValueError):
            await self.runtime.send(self.chat["id"], "r1")
        self.assertEqual(self.store.chat(self.chat["id"])["messages"], [])

    async def test_stop_marks_inflight_web_activity_stopped(self):
        async def searching(model, thinking, history, prompt, **options):
            yield {
                "text": "",
                "phase": "searching",
                "sources": [],
                "searches": [
                    {
                        "tool": "web_search",
                        "arguments": {"search_queries": ["docs"]},
                        "status": "running",
                        "error": None,
                    }
                ],
            }
            self.started.set()
            await self.release.wait()

        self.runtime.streamer = searching
        self.store.save_draft(self.chat["id"], "search", 1)
        await self.runtime.send(self.chat["id"], "web-stop")
        await self.started.wait()
        await self.runtime.stop()
        attempt = self.store.chat(self.chat["id"])["messages"][0]["attempts"][0]
        self.assertEqual(attempt["searches"][0]["status"], "stopped")
