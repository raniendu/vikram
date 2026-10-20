import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from vikram.desktop.store import Store


def png(width=3, height=2):
    out = io.BytesIO()
    Image.new("RGB", (width, height), "red").save(out, format="PNG")
    return out.getvalue()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "chat"
        self.store = Store(self.root)
        self.chat = self.store.create_chat("local:1")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_restart_recovers_partial_and_drafts(self):
        cid = self.chat["id"]
        self.store.save_draft(cid, "hello", 1)
        attempt = self.store.begin(cid, "local:1", None, "r1")
        self.store.checkpoint(attempt["id"], "partial", "reported:1")
        self.store.close()
        self.store = Store(self.root)
        detail = self.store.chat(cid)
        self.assertEqual(detail["messages"][0]["text"], "hello")
        saved = detail["messages"][0]["attempts"][0]
        self.assertEqual(saved["status"], "interrupted")
        self.assertEqual(saved["content"], "partial")
        self.assertEqual(saved["actual_model"], "reported:1")
        self.assertEqual(detail["draft"], "")

    def test_thinking_survives_restart_but_never_enters_followup_context(self):
        from vikram.desktop.history import inference_messages

        cid = self.chat["id"]
        self.store.save_draft(cid, "Hello", 1)
        attempt = self.store.begin(cid, "local:1", None, "first")
        self.store.checkpoint(
            attempt["id"],
            "Answer",
            "local:1",
            reasoning="Internal model thought",
            reasoning_truncated=True,
        )
        self.store.finish(attempt["id"], "complete")
        self.store.close()
        self.store = Store(self.root)
        saved = self.store.chat(cid)["messages"][0]["attempts"][0]
        self.assertEqual(saved["reasoning"], "Internal model thought")
        self.assertTrue(saved["reasoning_truncated"])
        self.store.save_draft(cid, "Continue", 3)
        next_attempt = self.store.begin(cid, "local:1", None, "next")
        history, prompt = inference_messages(
            self.store, cid, next_attempt["message_id"], False
        )
        self.assertNotIn("Internal model thought", str(history) + str(prompt))
        self.assertIn("Answer", str(history))

    def test_retry_does_not_duplicate_user_and_old_retry_is_rejected(self):
        cid = self.chat["id"]
        self.store.save_draft(cid, "first", 1)
        first = self.store.begin(cid, "one", False, "r1")
        self.store.finish(first["id"], "failed", "Unavailable")
        retry = self.store.begin(cid, "two", None, "r2", retry=True)
        self.assertEqual(retry["message_id"], first["message_id"])
        self.assertEqual(len(self.store.chat(cid)["messages"]), 1)
        self.store.finish(retry["id"], "complete")
        with self.assertRaises(ValueError):
            self.store.begin(cid, "two", None, "r3", retry=True)

    def test_folders_and_chat_isolation(self):
        folder = self.store.create_folder("Work")
        with self.assertRaises(ValueError):
            self.store.create_folder("work")
        self.store.update_chat(self.chat["id"], folder_id=folder["id"])
        other = self.store.create_chat("two")
        self.store.save_draft(other["id"], "separate", 1)
        self.store.delete_folder(folder["id"])
        self.assertIsNone(self.store.chat(self.chat["id"])["folder_id"])
        self.assertEqual(self.store.chat(other["id"])["draft"], "separate")
        self.assertEqual(self.store.chat(self.chat["id"])["draft"], "")

    def test_owned_images_and_failed_import(self):
        cid = self.chat["id"]
        original = Path(self.tmp.name) / "source.png"
        original.write_bytes(png())
        attachment = self.store.import_image(cid, original.name, original.read_bytes())
        with self.assertRaises(ValueError):
            self.store.import_image(cid, "bad.png", b"not an image")
        self.assertEqual(len(self.store.chat(cid)["attachments"]), 1)
        self.assertEqual(self.store.image_bytes(cid, attachment["id"]), png())
        other = self.store.create_chat("two")
        with self.assertRaises(ValueError):
            self.store.image_bytes(other["id"], attachment["id"])
        self.store.delete_chat(cid)
        self.assertTrue(original.exists())
        self.assertEqual(list((self.root / "attachments").iterdir()), [])

    def test_image_limits_and_sending_owns_draft(self):
        cid = self.chat["id"]
        for i in range(4):
            self.store.import_image(cid, f"{i}.png", png())
        with self.assertRaises(ValueError):
            self.store.import_image(cid, "fifth.png", png())
        attempt = self.store.begin(cid, "vision", None, "r1")
        self.assertEqual(self.store.chat(cid)["title"], "Image chat")
        self.assertTrue(
            all(
                a["message_id"] == attempt["message_id"]
                for a in self.store.chat(cid)["attachments"]
            )
        )
        with self.assertRaises(ValueError):
            self.store.delete_chat(cid)
        self.store.finish(attempt["id"], "stopped")
        with self.assertRaises(ValueError):
            self.store.checkpoint(attempt["id"], "late", None)

    def test_stale_draft_and_schema_version(self):
        cid = self.chat["id"]
        self.store.save_draft(cid, "new", 2)
        self.store.save_draft(cid, "old", 1)
        self.assertEqual(self.store.chat(cid)["draft"], "new")
        self.store.db.execute("PRAGMA user_version=999")
        self.store.close()
        with self.assertRaises(ValueError):
            Store(self.root)

    def test_documents_sources_and_attempt_web_setting_survive_restart(self):
        cid = self.chat["id"]
        self.assertEqual(self.chat["web"], "auto")
        item = self.store.import_attachment(cid, "notes.txt", b"Project sapphire")
        self.assertEqual(item["kind"], "document")
        self.assertIn("sapphire", item["extracted_text"])
        attempt = self.store.begin(cid, "local:1", None, "doc1", web="off")
        source = {
            "id": "S1",
            "url": "https://example.org",
            "title": "Example",
            "excerpt": "evidence",
            "retrieved_at": "now",
        }
        self.store.checkpoint(
            attempt["id"], "Answer", "local:1", sources=[source], searches=[]
        )
        self.store.finish(attempt["id"], "complete")
        self.store.close()
        self.store = Store(self.root)
        chat = self.store.chat(cid)
        self.assertEqual(chat["messages"][0]["attempts"][0]["web"], "off")
        self.assertEqual(chat["messages"][0]["attempts"][0]["sources"], [source])
        self.assertIn("sapphire", chat["attachments"][0]["extracted_text"])

    def test_version_three_migrates_without_losing_existing_replies(self):
        cid = self.chat["id"]
        self.store.save_draft(cid, "Saved question", 1)
        attempt = self.store.begin(cid, "local:1", None, "old")
        self.store.checkpoint(attempt["id"], "Saved answer", "local:1")
        self.store.finish(attempt["id"], "complete")
        for column in ("reasoning", "reasoning_truncated"):
            self.store.db.execute(f"ALTER TABLE attempts DROP COLUMN {column}")
        self.store.db.execute("PRAGMA user_version=3")
        self.store.db.commit()
        self.store.close()
        self.store = Store(self.root)
        saved = self.store.chat(cid)["messages"][0]["attempts"][0]
        self.assertEqual(saved["content"], "Saved answer")
        self.assertEqual(saved["status"], "complete")
        self.assertEqual(saved["reasoning"], "")
        self.assertIs(saved["reasoning_truncated"], False)

    def test_version_one_migrates_existing_chat_to_off(self):
        cid = self.chat["id"]
        self.store.import_image(cid, "photo.png", png())
        self.store.save_draft(cid, "preserved", 1)
        for table, columns in {
            "chats": ["web"],
            "attachments": [
                "kind",
                "extracted_text",
                "extraction_notice",
                "extraction_version",
            ],
            "attempts": [
                "web",
                "sources",
                "searches",
                "revision",
                "reasoning",
                "reasoning_truncated",
            ],
        }.items():
            for column in columns:
                self.store.db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        self.store.db.execute("PRAGMA user_version=1")
        self.store.db.commit()
        self.store.close()
        self.store = Store(self.root)
        chat = self.store.chat(cid)
        self.assertEqual(chat["web"], "off")
        self.assertEqual(chat["draft"], "preserved")
        self.assertEqual(chat["attachments"][0]["kind"], "image")

    def test_low_space_import_preserves_draft(self):
        from collections import namedtuple
        from unittest.mock import patch

        Usage = namedtuple("Usage", "total used free")
        with patch(
            "vikram.documents.shutil.disk_usage",
            return_value=Usage(100, 90, 19 * 1024**3),
        ):
            with self.assertRaisesRegex(ValueError, "20 GiB"):
                self.store.import_image(self.chat["id"], "photo.png", png())
        self.assertEqual(self.store.chat(self.chat["id"])["attachments"], [])
