"""Offline shared session ownership, branch, and persistence checks."""

import asyncio
import base64
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from vikram.desktop.store import Store


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.chat = self.store.create_chat("local:1")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def answer(self, prompt, answer):
        chat = self.store.chat(self.chat["id"])
        self.store.save_draft(chat["id"], prompt, chat["draft_revision"] + 1)
        attempt = self.store.begin(chat["id"], "local:1", None, "reply")
        self.store.checkpoint(attempt["id"], answer, "local:1")
        self.store.finish(attempt["id"], "complete")
        return attempt["message_id"]

    def test_opening_another_store_does_not_interrupt_owner(self):
        cid = self.chat["id"]
        self.store.save_draft(cid, "hello", 1)
        attempt = self.store.begin(cid, "local:1", None, "one")
        other = Store(self.root)
        try:
            self.assertEqual(
                other.chat(cid)["messages"][0]["attempts"][0]["status"], "running"
            )
            with self.assertRaisesRegex(ValueError, "another"):
                other.save_draft(cid, "overwrite", 4)
            second = other.create_chat("local:1")
            other.save_draft(second["id"], "different", 1)
            parallel = other.begin(second["id"], "local:1", None, "two")
            other.finish(parallel["id"], "complete")
        finally:
            other.close()
        self.store.finish(attempt["id"], "complete")

    def test_branches_preserve_other_history_and_clone(self):
        first = self.answer("first", "one")
        old_leaf = self.answer("second", "two")
        self.store.select_branch(self.chat["id"], first)
        new_leaf = self.answer("different", "three")
        self.assertEqual(
            [m["text"] for m in self.store.chat(self.chat["id"])["messages"]],
            ["first", "different"],
        )
        self.assertEqual(len(self.store.tree(self.chat["id"])), 3)
        self.store.select_branch(self.chat["id"], old_leaf)
        self.assertEqual(
            self.store.chat(self.chat["id"])["messages"][-1]["text"], "second"
        )
        clone = self.store.clone_chat(self.chat["id"])
        self.assertEqual([m["text"] for m in clone["messages"]], ["first", "second"])
        self.assertNotEqual(clone["messages"][0]["id"], first)
        self.assertEqual(clone["origin_chat_id"], self.chat["id"])
        self.assertEqual(len(self.store.tree(self.chat["id"])), 3)
        self.assertNotEqual(old_leaf, new_leaf)

    def test_crashed_process_is_recovered_without_interrupting_live_owner(self):
        code = """
import sys
from pathlib import Path
from vikram.desktop.store import Store
store = Store(Path(sys.argv[1]))
store.save_draft(sys.argv[2], 'work', 1)
store.begin(sys.argv[2], 'local:1', None, 'running')
print('ready', flush=True)
sys.stdin.read()
"""
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(self.root), self.chat["id"]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            other = Store(self.root)
            try:
                self.assertEqual(
                    other.chat(self.chat["id"])["messages"][0]["attempts"][0]["status"],
                    "running",
                )
                with self.assertRaisesRegex(ValueError, "another"):
                    other.claim_chat(self.chat["id"])
                process.kill()
                process.wait(timeout=10)
                other.claim_chat(self.chat["id"])
                self.assertEqual(
                    other.chat(self.chat["id"])["messages"][0]["attempts"][0]["status"],
                    "interrupted",
                )
            finally:
                other.close()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            process.stdin.close()
            process.stdout.close()

    def test_version_five_linear_history_migrates_without_loss(self):
        first = self.answer("first", "one")
        second = self.answer("second", "two")
        for column in (
            "workspace",
            "active_leaf",
            "origin_chat_id",
            "origin_message_id",
            "imported",
            "context_summary",
            "summary_through",
        ):
            self.store.db.execute(f"ALTER TABLE chats DROP COLUMN {column}")
        self.store.db.execute("ALTER TABLE messages DROP COLUMN parent_id")
        self.store.db.execute("PRAGMA user_version=5")
        self.store.db.commit()
        self.store.close()
        self.store = Store(self.root)
        chat = self.store.chat(self.chat["id"])
        self.assertEqual(chat["active_leaf"], second)
        self.assertEqual(chat["messages"][1]["parent_id"], first)
        self.assertEqual(
            [m["attempts"][0]["content"] for m in chat["messages"]], ["one", "two"]
        )

    def test_fork_copies_attachment_and_does_not_delete_source(self):
        attachment = self.store.import_attachment(self.chat["id"], "note.txt", b"facts")
        first = self.answer("read", "read it")
        self.answer("continue", "continued")
        fork = self.store.fork_chat(self.chat["id"], first, title="A fork")
        self.assertEqual(len(fork["messages"]), 1)
        copied = fork["attachments"][0]
        self.assertNotEqual(copied["id"], attachment["id"])
        self.assertEqual(self.store.image_bytes(fork["id"], copied["id"]), b"facts")
        self.store.delete_chat(fork["id"])
        self.assertEqual(
            self.store.image_bytes(self.chat["id"], attachment["id"]), b"facts"
        )


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_persisted_session_restores_without_thinking(self):
        from vikram.session import Session

        async def stream(*args, **kwargs):
            yield {
                "text": "answer",
                "actual_model": "local:1",
                "reasoning": "private thought",
                "sources": [],
                "activity": [],
            }

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            session = Session("local:1", store=store, streamer=stream)
            session.catalog.refresh = AsyncMock(return_value=[MODEL])
            events = [event async for event in session.reply("hello")]
            cid = session.session_id
            self.assertEqual(events[-1]["reasoning"], "private thought")
            saved = store.chat(cid)["messages"][0]["attempts"][0]
            self.assertEqual(saved["content"], "answer")
            self.assertEqual(saved["reasoning"], "")
            await session.close()
            restored = Session("local:1", store=store, chat_id=cid)
            self.assertEqual(restored.turns[0]["answer"], "answer")
            await restored.close()
            store.close()

    async def test_failed_compaction_keeps_history_and_queues(self):
        from vikram.session import Session

        session = Session("local:1")
        session.turns = [{"prompt": ["question"], "answer": "answer", "activity": []}]
        session.summarizer = AsyncMock(side_effect=ValueError("unavailable"))
        before = list(session.turns)
        with self.assertRaises(ValueError):
            await session.compact()
        self.assertEqual(session.turns, before)
        session.queue_input("steer", kind="steer")
        session.queue_input("later", kind="followup")
        self.assertEqual(session.take_steering(), ["steer"])
        self.assertEqual(session.take_followup(), "later")
        item = session.queue_input("unsent", kind="steer")
        session.restore_queued()
        self.assertEqual([q["text"] for q in session.queued_inputs], ["unsent"])
        session.edit_queued(item["id"], "changed")
        self.assertEqual(session.queued_inputs[0]["text"], "changed")
        await session.close()

    async def test_safe_roundtrip_export(self):
        from vikram.session_io import export_session, import_session

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            chat = store.create_chat("local:1")
            store.save_draft(chat["id"], "<script>alert(1)</script>", 1)
            attempt = store.begin(chat["id"], "local:1", None, "one")
            store.checkpoint(attempt["id"], "hello", "local:1", reasoning="SECRET")
            store.finish(attempt["id"], "complete")
            exported = export_session(store, chat["id"])
            self.assertNotIn("SECRET", exported)
            self.assertNotIn("reasoning", exported)
            restored = import_session(store, exported)
            self.assertTrue(restored["imported"])
            self.assertEqual(
                restored["messages"][0]["text"], "<script>alert(1)</script>"
            )
            html = export_session(store, chat["id"], format="html")
            self.assertIn("&lt;script&gt;", html)
            self.assertNotIn("<script>", html)
            store.close()

    async def test_delivered_steering_is_saved_and_unsent_queue_survives_stop(self):
        from vikram.session import Session

        calls = []

        async def stream(*args, request_boundary=None, **kwargs):
            calls.append(request_boundary())
            yield {
                "text": "partial",
                "actual_model": "local:1",
                "reasoning": "private",
                "activity": [
                    {
                        "kind": "coding",
                        "status": "complete",
                        "operation": "write",
                        "path": "done.txt",
                    }
                ],
            }
            await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            session = Session("local:1", store=store, streamer=stream)
            session.catalog.refresh = AsyncMock(return_value=[MODEL])
            session.queue_input("include detail")
            session.queue_input("next question", kind="followup")
            iterator = session.reply("hello")
            async for event in iterator:
                if "text" in event:
                    break
            await iterator.aclose()
            self.assertEqual(calls, [["include detail"]])
            saved = store.chat(session.session_id)["messages"][0]
            self.assertIn("include detail", saved["text"])
            self.assertEqual(saved["attempts"][0]["status"], "stopped")
            self.assertEqual(saved["attempts"][0]["reasoning"], "")
            self.assertIsNone(session.turns[0]["answer"])
            self.assertIn("done.txt", str(session.history()))
            self.assertNotIn("partial", str(session.history()))
            self.assertEqual(
                [q["text"] for q in session.drain_queued()], ["next question"]
            )
            self.assertEqual(session.queued_inputs, [])
            await session.close()
            store.close()

    async def test_auto_compaction_uses_known_budget_without_deleting_turns(self):
        from vikram.session import Session

        calls = []

        async def stream(model, thinking, history, prompt, **kwargs):
            calls.append(history)
            yield {"text": "new answer", "actual_model": model}

        session = Session(
            "local:1",
            streamer=stream,
            summarizer=AsyncMock(return_value="compact memory"),
        )
        session.catalog.refresh = AsyncMock(
            return_value=[{**MODEL, "context_length": 100}]
        )
        session.turns = [
            {"prompt": [str(i) + "x" * 200], "answer": "y" * 200} for i in range(4)
        ]
        async for _ in session.reply("continue"):
            pass
        self.assertEqual(len(session.turns), 5)
        self.assertEqual(session.context_state()["compacted_turns"], 2)
        self.assertIn("compact memory", str(calls[0]))
        self.assertNotIn("0xxx", str(calls[0]))
        await session.close()

    async def test_invalid_import_is_atomic_and_permissions_are_not_restored(self):
        from vikram.session_io import import_session

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            header = {
                "type": "session",
                "format": "vikram-session",
                "version": 1,
                "title": "Imported",
                "active_leaf": "one",
                "workspace": "/private",
                "trusted": True,
            }
            message = {
                "type": "message",
                "id": "one",
                "parent_id": None,
                "text": "hello",
                "attempts": [],
            }
            text = "\n".join(
                map(
                    json.dumps,
                    [
                        header,
                        message,
                        {
                            "type": "attachment",
                            "message_id": "one",
                            "name": "bad.png",
                            "kind": "image",
                            "data": "AA==",
                        },
                    ],
                )
            )
            with self.assertRaises(ValueError):
                import_session(store, text)
            self.assertEqual(store.list_chats(), [])
            imported = import_session(
                store, "\n".join(map(json.dumps, [header, message]))
            )
            self.assertIsNone(imported["workspace"])
            self.assertEqual(imported["imported"], 1)
            self.assertNotIn("trusted", imported)
            store.close()


MODEL = {
    "name": "local:1",
    "available": True,
    "vision": False,
    "tools": False,
    "thinking": [],
    "thinking_default": None,
    "context_length": 10000,
}


class SessionImportTests(unittest.IsolatedAsyncioTestCase):
    def transcript(self, *, model="local:1", documents=()):
        records = [
            {
                "type": "session",
                "format": "vikram-session",
                "version": 1,
                "title": "Import",
                "model": model,
                "active_leaf": "one",
            },
            {
                "type": "message",
                "id": "one",
                "parent_id": None,
                "text": "Read these documents",
                "attempts": [
                    {
                        "status": "complete",
                        "content": "Historical answer",
                    }
                ],
            },
        ]
        records.extend(
            {
                "type": "attachment",
                "message_id": "one",
                "name": name,
                "kind": "document",
                "data": base64.b64encode(data).decode("ascii"),
            }
            for name, data in documents
        )
        return "\n".join(map(json.dumps, records))

    async def test_imported_cloud_model_is_only_historical_metadata(self):
        from vikram.agent import DEFAULT_MODEL
        from vikram.session import Session
        from vikram.session_io import import_session

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            imported = import_session(
                store, self.transcript(model=" openrouter:vendor/model ")
            )
            self.assertEqual(imported["model"], DEFAULT_MODEL)
            self.assertEqual(
                imported["messages"][0]["attempts"][0]["requested_model"],
                " openrouter:vendor/model ",
            )
            session = Session(
                "openrouter:another/model", store=store, chat_id=imported["id"]
            )
            self.assertEqual(session.model, DEFAULT_MODEL)
            await session.close()
            store.close()

    async def test_sync_import_document_extraction_has_worker_timeout(self):
        from vikram.session_io import import_session

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            with patch("vikram.documents.EXTRACT_TIMEOUT", 0.001):
                with self.assertRaisesRegex(ValueError, "too long"):
                    import_session(
                        store, self.transcript(documents=[("hello.txt", b"hello")])
                    )
            self.assertEqual(store.list_chats(), [])
            self.assertEqual(list(store.files.iterdir()), [])
            store.close()

    async def test_async_import_uses_worker_and_invalid_document_is_atomic(self):
        from vikram import session_io

        importer = getattr(session_io, "import_session_async", None)
        self.assertTrue(
            callable(importer), "Imports need a cancellable async entry point"
        )
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            with patch(
                "vikram.documents.extract_document",
                side_effect=AssertionError("Parser ran in host"),
            ):
                imported = await importer(
                    store, self.transcript(documents=[("hello.txt", b"hello")])
                )
                self.assertEqual(
                    imported["attachments"][0]["extracted_text"], "1: hello\n"
                )
                before = set(store.files.iterdir())
                with self.assertRaises(ValueError):
                    await importer(
                        store,
                        self.transcript(
                            documents=[("ok.txt", b"ok"), ("bad.pdf", b"broken")]
                        ),
                    )
                self.assertEqual(len(store.list_chats()), 1)
                self.assertEqual(set(store.files.iterdir()), before)
            store.close()

    async def test_cancel_import_reaps_worker_keeps_loop_live_and_store_empty(self):
        from vikram import session_io

        importer = getattr(session_io, "import_session_async", None)
        self.assertTrue(
            callable(importer), "Imports need a cancellable async entry point"
        )
        spawn = asyncio.create_subprocess_exec
        started = asyncio.Event()
        processes = []

        async def slow_worker(*command, **kwargs):
            process = await spawn(
                sys.executable,
                "-c",
                "import sys,time;sys.stdin.buffer.read();time.sleep(60)",
                **kwargs,
            )
            processes.append(process)
            started.set()
            return process

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            with patch("vikram.documents.asyncio.create_subprocess_exec", slow_worker):
                pending = asyncio.create_task(
                    importer(
                        store, self.transcript(documents=[("hello.txt", b"hello")])
                    )
                )
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    await asyncio.sleep(0)
                    self.assertFalse(pending.done())
                finally:
                    pending.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await pending
            self.assertIsNotNone(processes[0].returncode)
            self.assertEqual(store.list_chats(), [])
            self.assertEqual(list(store.files.iterdir()), [])
            store.close()


class UncertainEffectTests(unittest.TestCase):
    def test_stopped_effect_is_not_replayed_as_an_unanswered_request(self):
        from vikram.session import history_messages

        turns = [
            {
                "prompt": ["Update the file"],
                "answer": None,
                "activity": [
                    {
                        "kind": "coding",
                        "tool": "bash",
                        "status": "stopped",
                        "arguments": {"command": "touch done.txt"},
                    }
                ],
            }
        ]
        history = str(history_messages(turns))
        self.assertIn("done.txt", history)
        self.assertIn("Inspect the current state before retrying", history)


class CompactionEffectTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_request_retains_uncertain_action_warning(self):
        from vikram.session import Session

        observed = []

        async def summary(model, thinking, history, prompt, **kwargs):
            observed.extend(prompt)
            yield {"text": "An action may have completed; inspect before retrying."}

        session = Session("local:1", streamer=summary)
        session.turns = [
            {
                "prompt": ["Create a file"],
                "answer": None,
                "activity": [
                    {
                        "kind": "coding",
                        "tool": "bash",
                        "status": "stopped",
                        "arguments": {"command": "touch done.txt"},
                    }
                ],
            }
        ]
        await session.compact()
        self.assertIn("done.txt", str(observed))
        self.assertIn("Inspect the current state before retrying", str(observed))
        self.assertEqual(len(session.turns), 1)
