"""Transactional desktop records and private attachment ownership."""

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from vikram.conversation import DEFAULT_WEB
from vikram.documents import ensure_space, extract_document

from .attachments import MAX_IMAGES, validate_image

TERMINAL = {"complete", "failed", "stopped", "interrupted"}


def uid() -> str:
    return uuid4().hex


def now() -> float:
    return time.time()


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        self.files = root / "attachments"
        self.files.mkdir(exist_ok=True, mode=0o700)
        self.files.chmod(0o700)
        self.db = sqlite3.connect(root / "chats.sqlite3")
        (root / "chats.sqlite3").chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=3000")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, 4):
            self.db.close()
            raise ValueError("This chat library needs a newer version of Vikram.")
        self.db.executescript("""
        BEGIN;
        CREATE TABLE IF NOT EXISTS folders (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chats (
            id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT 'New chat',
            custom_title INTEGER NOT NULL DEFAULT 0,
            folder_id TEXT REFERENCES folders(id) ON DELETE SET NULL,
            model TEXT, thinking TEXT NOT NULL DEFAULT '{}',
            draft TEXT NOT NULL DEFAULT '', draft_revision INTEGER NOT NULL DEFAULT 0,
            created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
            text TEXT NOT NULL, created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attempts (
            id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
            message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            request_id TEXT NOT NULL, requested_model TEXT NOT NULL, actual_model TEXT,
            thinking TEXT NOT NULL, status TEXT NOT NULL, content TEXT NOT NULL DEFAULT '',
            error TEXT, created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attachments (
            id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
            message_id TEXT REFERENCES messages(id) ON DELETE CASCADE,
            name TEXT NOT NULL, media_type TEXT NOT NULL,
            width INTEGER NOT NULL, height INTEGER NOT NULL, size INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id);
        CREATE INDEX IF NOT EXISTS attempts_message ON attempts(message_id);
        CREATE INDEX IF NOT EXISTS attachments_chat ON attachments(chat_id);
        COMMIT;
        """)
        if version < 2:
            self.db.executescript("""
            BEGIN;
            ALTER TABLE chats ADD COLUMN web TEXT NOT NULL DEFAULT 'off';
            ALTER TABLE attachments ADD COLUMN kind TEXT NOT NULL DEFAULT 'image';
            ALTER TABLE attachments ADD COLUMN extracted_text TEXT NOT NULL DEFAULT '';
            ALTER TABLE attachments ADD COLUMN extraction_notice TEXT;
            ALTER TABLE attachments ADD COLUMN extraction_version INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE attempts ADD COLUMN web TEXT NOT NULL DEFAULT 'off';
            ALTER TABLE attempts ADD COLUMN sources TEXT NOT NULL DEFAULT '[]';
            ALTER TABLE attempts ADD COLUMN searches TEXT NOT NULL DEFAULT '[]';
            PRAGMA user_version=2;
            COMMIT;
            """)
        if version < 3:
            self.db.executescript("""
            BEGIN;
            ALTER TABLE attempts ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
            PRAGMA user_version=3;
            COMMIT;
            """)
        if version < 4:
            self.db.executescript("""
            BEGIN;
            ALTER TABLE attempts ADD COLUMN reasoning TEXT NOT NULL DEFAULT '';
            ALTER TABLE attempts ADD COLUMN reasoning_truncated INTEGER NOT NULL DEFAULT 0;
            PRAGMA user_version=4;
            COMMIT;
            """)
        with self.db:
            self.db.execute(
                "UPDATE attempts SET status='interrupted', error='The app closed before this reply finished.' WHERE status='running'"
            )
        self._collect_files()

    def close(self) -> None:
        self.db.close()

    def _one(self, sql: str, args: tuple = ()) -> dict:
        row = self.db.execute(sql, args).fetchone()
        if row is None:
            raise ValueError("That item is no longer available.")
        return dict(row)

    def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        return [dict(row) for row in self.db.execute(sql, args)]

    def snapshot(self) -> dict:
        chats = self._rows("SELECT * FROM chats ORDER BY updated DESC, id")
        for chat in chats:
            chat["thinking"] = json.loads(chat["thinking"])
        return {
            "chats": chats,
            "folders": self._rows("SELECT id,name FROM folders ORDER BY name_key"),
            "preferences": {
                r["key"]: json.loads(r["value"])
                for r in self._rows("SELECT * FROM preferences")
            },
        }

    def chat(self, chat_id: str) -> dict:
        chat = self._one("SELECT * FROM chats WHERE id=?", (chat_id,))
        chat["thinking"] = json.loads(chat["thinking"])
        chat["attachments"] = self._rows(
            "SELECT * FROM attachments WHERE chat_id=? ORDER BY rowid", (chat_id,)
        )
        chat["messages"] = self._rows(
            "SELECT * FROM messages WHERE chat_id=? ORDER BY rowid", (chat_id,)
        )
        for message in chat["messages"]:
            message["attempts"] = self._rows(
                "SELECT * FROM attempts WHERE message_id=? ORDER BY rowid",
                (message["id"],),
            )
            for attempt in message["attempts"]:
                for key in ("thinking", "sources", "searches"):
                    attempt[key] = json.loads(attempt[key])
                attempt["reasoning_truncated"] = bool(attempt["reasoning_truncated"])
        return chat

    def create_chat(self, model: str | None, folder_id: str | None = None) -> dict:
        cid, timestamp = uid(), now()
        if folder_id is not None:
            self._one("SELECT id FROM folders WHERE id=?", (folder_id,))
        with self.db:
            self.db.execute(
                "INSERT INTO chats(id,model,folder_id,created,updated,web) VALUES(?,?,?,?,?,?)",
                (cid, model, folder_id, timestamp, timestamp, DEFAULT_WEB),
            )
        return self.chat(cid)

    def update_chat(self, chat_id: str, **fields: Any) -> dict:
        self._one("SELECT id FROM chats WHERE id=?", (chat_id,))
        if not fields or set(fields) - {
            "title",
            "folder_id",
            "model",
            "thinking",
        }:
            raise ValueError("Unsupported chat change.")
        if "title" in fields:
            fields["title"] = str(fields["title"]).strip()[:160]
            if not fields["title"]:
                raise ValueError("Enter a chat title.")
            fields["custom_title"] = 1
        if fields.get("folder_id") is not None:
            self._one("SELECT id FROM folders WHERE id=?", (fields["folder_id"],))
        if "thinking" in fields:
            fields["thinking"] = json.dumps(fields["thinking"])
        # Selection and drafts must not reorder the conversation list.
        with self.db:
            self.db.execute(
                f"UPDATE chats SET {','.join(f'{key}=?' for key in fields)} WHERE id=?",
                (*fields.values(), chat_id),
            )
        return self.chat(chat_id)

    def save_draft(self, chat_id: str, text: str, revision: int) -> None:
        if len(text) > 1_000_000:
            raise ValueError(
                "This message is too long. Split it into smaller messages."
            )
        self._one("SELECT id FROM chats WHERE id=?", (chat_id,))
        with self.db:
            self.db.execute(
                "UPDATE chats SET draft=?,draft_revision=? WHERE id=? AND draft_revision<?",
                (text, revision, chat_id, revision),
            )

    def set_preference(self, key: str, value: Any) -> None:
        if key not in {"appearance", "last_model", "selected_chat", "sidebar"}:
            raise ValueError("Unknown preference.")
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO preferences VALUES(?,?)",
                (key, json.dumps(value)),
            )

    def create_folder(self, name: str) -> dict:
        fid = uid()
        self._folder(fid, name, create=True)
        return {"id": fid, "name": name.strip()}

    def _folder(self, fid: str, name: str, create: bool = False) -> None:
        name = name.strip()
        if not name or len(name) > 80:
            raise ValueError("Use a folder name between 1 and 80 characters.")
        if not create:
            self._one("SELECT id FROM folders WHERE id=?", (fid,))
        try:
            with self.db:
                if create:
                    self.db.execute(
                        "INSERT INTO folders VALUES(?,?,?)",
                        (fid, name, name.casefold()),
                    )
                else:
                    self.db.execute(
                        "UPDATE folders SET name=?,name_key=? WHERE id=?",
                        (name, name.casefold(), fid),
                    )
        except sqlite3.IntegrityError:
            raise ValueError("A folder with that name already exists.") from None

    def rename_folder(self, fid: str, name: str) -> None:
        self._folder(fid, name)

    def delete_folder(self, fid: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM folders WHERE id=?", (fid,))

    def delete_chat(self, chat_id: str) -> None:
        if self.db.execute(
            "SELECT 1 FROM attempts WHERE chat_id=? AND status='running'", (chat_id,)
        ).fetchone():
            raise ValueError("Stop this reply before deleting its chat.")
        with self.db:
            self.db.execute("DELETE FROM chats WHERE id=?", (chat_id,))
        self._collect_files()

    def _collect_files(self) -> None:
        live = {row["id"] for row in self._rows("SELECT id FROM attachments")}
        for path in self.files.iterdir():
            if re.fullmatch(r"[0-9a-f]{32}\.(blob|tmp)", path.name) and (
                path.suffix == ".tmp" or path.stem not in live
            ):
                path.unlink(missing_ok=True)

    def import_image(self, chat_id: str, name: str, data: bytes) -> dict:
        media, width, height = validate_image(data)
        return self.import_attachment(
            chat_id,
            name,
            data,
            extracted={
                "kind": "image",
                "media_type": media,
                "width": width,
                "height": height,
                "text": "",
                "notice": None,
                "extraction_version": 0,
            },
        )

    def import_attachment(
        self, chat_id: str, name: str, data: bytes, *, extracted: dict | None = None
    ) -> dict:
        self._one("SELECT id FROM chats WHERE id=?", (chat_id,))
        count = self.db.execute(
            "SELECT count(*) FROM attachments WHERE chat_id=? AND message_id IS NULL",
            (chat_id,),
        ).fetchone()[0]
        if count >= MAX_IMAGES:
            raise ValueError("Attach up to four files per message.")
        if extracted is None:
            extracted = extract_document(name, data)
        ensure_space(self.root, len(data) + 50 * 1024**2)
        aid = uid()
        temporary, final = self.files / f"{aid}.tmp", self.files / f"{aid}.blob"
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            temporary.replace(final)
            with self.db:
                self.db.execute(
                    "INSERT INTO attachments(id,chat_id,message_id,name,media_type,width,height,size,kind,extracted_text,extraction_notice,extraction_version) VALUES(?,?,NULL,?,?,?,?,?,?,?,?,?)",
                    (
                        aid,
                        chat_id,
                        Path(name).name[:180],
                        extracted["media_type"],
                        extracted["width"],
                        extracted["height"],
                        len(data),
                        extracted["kind"],
                        extracted["text"],
                        extracted["notice"],
                        extracted["extraction_version"],
                    ),
                )
        except BaseException:
            temporary.unlink(missing_ok=True)
            final.unlink(missing_ok=True)
            raise
        return self._one("SELECT * FROM attachments WHERE id=?", (aid,))

    def image_bytes(self, chat_id: str, aid: str) -> bytes:
        self._one("SELECT id FROM attachments WHERE id=? AND chat_id=?", (aid, chat_id))
        return (self.files / f"{aid}.blob").read_bytes()

    def remove_image(self, chat_id: str, aid: str) -> None:
        self._one(
            "SELECT id FROM attachments WHERE id=? AND chat_id=? AND message_id IS NULL",
            (aid, chat_id),
        )
        with self.db:
            self.db.execute("DELETE FROM attachments WHERE id=?", (aid,))
        (self.files / f"{aid}.blob").unlink(missing_ok=True)

    def begin(
        self,
        chat_id: str,
        model: str,
        thinking: bool | str | None,
        request_id: str,
        retry: bool = False,
        web: str = DEFAULT_WEB,
    ) -> dict:
        chat = self.chat(chat_id)
        aid, timestamp = uid(), now()
        if self.db.execute("SELECT 1 FROM attempts WHERE status='running'").fetchone():
            raise ValueError("Another reply is still running.")
        with self.db:
            if retry:
                if not chat["messages"]:
                    raise ValueError("There is no reply to retry.")
                latest = chat["messages"][-1]
                attempts = latest["attempts"]
                if not attempts or any(
                    a["status"] in {"complete", "running"} for a in attempts
                ):
                    raise ValueError(
                        "Only the latest failed or stopped reply can be retried."
                    )
                mid = latest["id"]
            else:
                text = chat["draft"].strip()
                images = [a for a in chat["attachments"] if a["message_id"] is None]
                if not text and not images:
                    raise ValueError("Type a message or attach a file.")
                mid = uid()
                self.db.execute(
                    "INSERT INTO messages VALUES(?,?,?,?)",
                    (mid, chat_id, text, timestamp),
                )
                self.db.execute(
                    "UPDATE attachments SET message_id=? WHERE chat_id=? AND message_id IS NULL",
                    (mid, chat_id),
                )
                self.db.execute(
                    "UPDATE chats SET draft='',draft_revision=draft_revision+1 WHERE id=?",
                    (chat_id,),
                )
                if not chat["messages"] and not chat["custom_title"]:
                    title = (
                        " ".join(text.split())[:70]
                        if text
                        else (
                            "Document chat"
                            if any(a["kind"] == "document" for a in images)
                            else "Image chat"
                        )
                    )
                    self.db.execute(
                        "UPDATE chats SET title=? WHERE id=?", (title, chat_id)
                    )
            self.db.execute(
                "INSERT INTO attempts(id,chat_id,message_id,request_id,requested_model,thinking,status,created,updated,web) VALUES(?,?,?,?,?,?,'running',?,?,?)",
                (
                    aid,
                    chat_id,
                    mid,
                    request_id,
                    model,
                    json.dumps(thinking),
                    timestamp,
                    timestamp,
                    web,
                ),
            )
            self.db.execute(
                "UPDATE chats SET updated=? WHERE id=?", (timestamp, chat_id)
            )
        attempt = self._one("SELECT * FROM attempts WHERE id=?", (aid,))
        attempt["reasoning_truncated"] = bool(attempt["reasoning_truncated"])
        return attempt

    def checkpoint(
        self,
        attempt_id: str,
        content: str,
        actual_model: str | None,
        *,
        sources: list | None = None,
        searches: list | None = None,
        revision: int | None = None,
        reasoning: str | None = None,
        reasoning_truncated: bool | None = None,
    ) -> None:
        with self.db:
            cur = self.db.execute(
                "UPDATE attempts SET content=?,actual_model=COALESCE(?,actual_model),sources=COALESCE(?,sources),searches=COALESCE(?,searches),revision=COALESCE(?,revision+1),reasoning=COALESCE(?,reasoning),reasoning_truncated=COALESCE(?,reasoning_truncated),updated=? WHERE id=? AND status='running'",
                (
                    content,
                    actual_model,
                    json.dumps(sources) if sources is not None else None,
                    json.dumps(searches) if searches is not None else None,
                    revision,
                    reasoning,
                    reasoning_truncated,
                    now(),
                    attempt_id,
                ),
            )
            if cur.rowcount != 1:
                raise ValueError("This reply is no longer running.")

    def finish(self, attempt_id: str, status: str, error: str | None = None) -> None:
        if status not in TERMINAL:
            raise ValueError("Invalid reply state.")
        with self.db:
            self.db.execute(
                "UPDATE attempts SET status=?,error=?,updated=? WHERE id=? AND status='running'",
                (status, error, now(), attempt_id),
            )
