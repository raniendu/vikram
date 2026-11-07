"""Transactional desktop records and private attachment ownership."""

import fcntl
import json
import os
import re
import sqlite3
import time
from contextlib import ExitStack, contextmanager
from functools import wraps
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


def session_mutation(method):
    """Serialize session edits with active replies, including other processes."""

    @wraps(method)
    def guarded(self, chat_id, *args, **kwargs):
        with self.mutation(chat_id):
            return method(self, chat_id, *args, **kwargs)

    return guarded


def file_mutation(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self.file_lock():
            return method(self, *args, **kwargs)

    return guarded


def attempt_mutation(method):
    @wraps(method)
    def guarded(self, attempt_id, *args, **kwargs):
        chat_id = self._one("SELECT chat_id FROM attempts WHERE id=?", (attempt_id,))[
            "chat_id"
        ]
        with self.mutation(chat_id):
            return method(self, attempt_id, *args, **kwargs)

    return guarded


class Store:
    def __init__(self, root: Path):
        self.root = root = Path(root)
        self._locks: dict[str, int] = {}
        self._attempt_locks: set[str] = set()
        self._files_locked = 0
        self._closed = False
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        self.files = root / "attachments"
        self.files.mkdir(exist_ok=True, mode=0o700)
        self.files.chmod(0o700)
        self.locks = root / "locks"
        self.locks.mkdir(exist_ok=True, mode=0o700)
        self.locks.chmod(0o700)
        # A second connection must not inspect a half-migrated schema.
        migration_fd = os.open(root / "migration.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(migration_fd, fcntl.LOCK_EX)
            self._open_database()
        finally:
            os.close(migration_fd)
        self._recover()
        self._collect_files()

    def _open_database(self) -> None:
        root = self.root
        self.db = sqlite3.connect(root / "chats.sqlite3")
        (root / "chats.sqlite3").chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=3000")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, 4, 5, 6, 7):
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
        if version < 5:
            self.db.executescript("""
            BEGIN;
            ALTER TABLE chats ADD COLUMN mode TEXT NOT NULL DEFAULT 'normal';
            ALTER TABLE attempts ADD COLUMN mode TEXT NOT NULL DEFAULT 'normal';
            ALTER TABLE attempts ADD COLUMN activity TEXT NOT NULL DEFAULT '[]';
            PRAGMA user_version=5;
            COMMIT;
            """)
        if version < 6:
            with self.db:
                columns = {
                    "chats": {
                        "workspace": "TEXT",
                        "active_leaf": "TEXT",
                        "origin_chat_id": "TEXT",
                        "origin_message_id": "TEXT",
                        "imported": "INTEGER NOT NULL DEFAULT 0",
                        "context_summary": "TEXT NOT NULL DEFAULT ''",
                        "summary_through": "TEXT",
                    },
                    "messages": {"parent_id": "TEXT"},
                }
                new_tree = "parent_id" not in {
                    row[1] for row in self.db.execute("PRAGMA table_info(messages)")
                }
                for table, fields in columns.items():
                    present = {
                        row[1] for row in self.db.execute(f"PRAGMA table_info({table})")
                    }
                    for name, kind in fields.items():
                        if name not in present:
                            self.db.execute(
                                f"ALTER TABLE {table} ADD COLUMN {name} {kind}"
                            )
                if new_tree:
                    for row in self.db.execute("SELECT id FROM chats").fetchall():
                        parent = None
                        for message in self.db.execute(
                            "SELECT id FROM messages WHERE chat_id=? ORDER BY rowid",
                            (row[0],),
                        ).fetchall():
                            self.db.execute(
                                "UPDATE messages SET parent_id=? WHERE id=?",
                                (parent, message[0]),
                            )
                            parent = message[0]
                        self.db.execute(
                            "UPDATE chats SET active_leaf=? WHERE id=?",
                            (parent, row[0]),
                        )
                self.db.execute("PRAGMA user_version=6")
        if version < 7:
            with self.db:
                if "usage" not in {
                    row[1] for row in self.db.execute("PRAGMA table_info(attempts)")
                }:
                    self.db.execute(
                        "ALTER TABLE attempts ADD COLUMN usage TEXT NOT NULL DEFAULT '{}'"
                    )
                self.db.execute("PRAGMA user_version=7")

    def claim_chat(self, chat_id: str) -> bool:
        """Hold ownership until release/close; OS locks survive neither PID reuse nor crash."""
        if chat_id in self._locks:
            return False
        self._one("SELECT id FROM chats WHERE id=?", (chat_id,))
        if not re.fullmatch(r"[0-9a-f]{32}", chat_id):
            raise ValueError("Invalid session identifier.")
        fd = os.open(self.locks / f"{chat_id}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise ValueError(
                "This session is active in another Vikram instance."
            ) from None
        self._locks[chat_id] = fd
        try:
            with self.db:
                self.db.execute(
                    "UPDATE attempts SET status='interrupted', error='Vikram closed before this reply finished.' WHERE chat_id=? AND status='running'",
                    (chat_id,),
                )
        except BaseException:
            self.release_chat(chat_id)
            raise
        return True

    def release_chat(self, chat_id: str) -> None:
        fd = self._locks.pop(chat_id, None)
        if fd is not None:
            os.close(fd)
        self._attempt_locks.discard(chat_id)

    @contextmanager
    def mutation(self, chat_id: str):
        acquired = self.claim_chat(chat_id)
        try:
            yield
        finally:
            if acquired:
                self.release_chat(chat_id)

    @contextmanager
    def file_lock(self):
        """Prevent garbage collection from racing an attachment import."""
        if self._files_locked:
            yield
            return
        fd = os.open(self.root / "attachments.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._files_locked += 1
            yield
        finally:
            self._files_locked -= 1
            os.close(fd)

    def _recover(self) -> None:
        for row in self._rows(
            "SELECT DISTINCT chat_id FROM attempts WHERE status='running'"
        ):
            try:
                with self.mutation(row["chat_id"]):
                    with self.db:
                        self.db.execute(
                            "UPDATE attempts SET status='interrupted', error='Vikram closed before this reply finished.' WHERE chat_id=? AND status='running'",
                            (row["chat_id"],),
                        )
            except ValueError:
                # A live owner is authoritative, even in this same process.
                continue

    def close(self) -> None:
        if self._closed:
            return
        self.db.close()
        for chat_id in list(self._locks):
            self.release_chat(chat_id)
        self._closed = True

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
        messages = self._rows(
            "SELECT * FROM messages WHERE chat_id=? ORDER BY rowid", (chat_id,)
        )
        by_id = {m["id"]: m for m in messages}
        path, leaf = [], chat["active_leaf"]
        visited = set()
        while leaf is not None:
            if leaf not in by_id or leaf in visited:
                raise ValueError("This session's branch history is inconsistent.")
            visited.add(leaf)
            path.append(by_id[leaf])
            leaf = by_id[leaf]["parent_id"]
        chat["messages"] = list(reversed(path))
        for message in chat["messages"]:
            message["attempts"] = self._rows(
                "SELECT * FROM attempts WHERE message_id=? ORDER BY rowid",
                (message["id"],),
            )
            for attempt in message["attempts"]:
                for key in ("thinking", "sources", "searches", "activity", "usage"):
                    attempt[key] = json.loads(attempt[key])
                attempt["reasoning_truncated"] = bool(attempt["reasoning_truncated"])
        return chat

    def create_chat(
        self,
        model: str | None,
        folder_id: str | None = None,
        *,
        workspace: str | Path | None = None,
    ) -> dict:
        cid, timestamp = uid(), now()
        if folder_id is not None:
            self._one("SELECT id FROM folders WHERE id=?", (folder_id,))
        with self.db:
            self.db.execute(
                "INSERT INTO chats(id,model,folder_id,created,updated,web,workspace) VALUES(?,?,?,?,?,?,?)",
                (
                    cid,
                    model,
                    folder_id,
                    timestamp,
                    timestamp,
                    DEFAULT_WEB,
                    str(Path(workspace).resolve()) if workspace else None,
                ),
            )
        return self.chat(cid)

    @session_mutation
    def update_chat(self, chat_id: str, **fields: Any) -> dict:
        self._one("SELECT id FROM chats WHERE id=?", (chat_id,))
        if not fields or set(fields) - {
            "title",
            "folder_id",
            "model",
            "thinking",
            "mode",
            "workspace",
        }:
            raise ValueError("Unsupported chat change.")
        if "mode" in fields and fields["mode"] not in ("normal", "plan"):
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
        if fields.get("workspace") is not None:
            fields["workspace"] = str(Path(fields["workspace"]).resolve())
        # Selection and drafts must not reorder the conversation list.
        with self.db:
            self.db.execute(
                f"UPDATE chats SET {','.join(f'{key}=?' for key in fields)} WHERE id=?",
                (*fields.values(), chat_id),
            )
        return self.chat(chat_id)

    @session_mutation
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
        with ExitStack() as owners:
            for chat in self._rows(
                "SELECT id FROM chats WHERE folder_id=? ORDER BY id", (fid,)
            ):
                owners.enter_context(self.mutation(chat["id"]))
            with self.db:
                self.db.execute("DELETE FROM folders WHERE id=?", (fid,))

    @session_mutation
    def delete_chat(self, chat_id: str) -> None:
        if self.db.execute(
            "SELECT 1 FROM attempts WHERE chat_id=? AND status='running'", (chat_id,)
        ).fetchone():
            raise ValueError("Stop this reply before deleting its chat.")
        with self.db:
            self.db.execute("DELETE FROM chats WHERE id=?", (chat_id,))
        self._collect_files()

    @file_mutation
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

    @session_mutation
    @file_mutation
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

    @session_mutation
    @file_mutation
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
        mode: str = "normal",
        prompt: str | None = None,
    ) -> dict:
        acquired = self.claim_chat(chat_id)
        try:
            attempt = self._begin(
                chat_id, model, thinking, request_id, retry, web, mode, prompt
            )
        except BaseException:
            if acquired:
                self.release_chat(chat_id)
            raise
        if acquired:
            self._attempt_locks.add(chat_id)
        return attempt

    def _begin(
        self, chat_id, model, thinking, request_id, retry, web, mode, prompt=None
    ) -> dict:
        chat = self.chat(chat_id)
        aid, timestamp = uid(), now()
        if self.db.execute(
            "SELECT 1 FROM attempts WHERE chat_id=? AND status='running'", (chat_id,)
        ).fetchone():
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
                text = (chat["draft"] if prompt is None else prompt).strip()
                images = (
                    [a for a in chat["attachments"] if a["message_id"] is None]
                    if prompt is None
                    else []
                )
                if not text and not images:
                    raise ValueError("Type a message or attach a file.")
                mid = uid()
                self.db.execute(
                    "INSERT INTO messages(id,chat_id,text,created,parent_id) VALUES(?,?,?,?,?)",
                    (mid, chat_id, text, timestamp, chat["active_leaf"]),
                )
                self.db.execute(
                    "UPDATE chats SET active_leaf=? WHERE id=?", (mid, chat_id)
                )
                if prompt is None:
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
                "INSERT INTO attempts(id,chat_id,message_id,request_id,requested_model,thinking,status,created,updated,web,mode) VALUES(?,?,?,?,?,?,'running',?,?,?,?)",
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
                    mode,
                ),
            )
            self.db.execute(
                "UPDATE chats SET updated=? WHERE id=?", (timestamp, chat_id)
            )
        attempt = self._one("SELECT * FROM attempts WHERE id=?", (aid,))
        attempt["reasoning_truncated"] = bool(attempt["reasoning_truncated"])
        return attempt

    def import_run(
        self, title: str, prompt: str, run: dict, *, model: str | None
    ) -> dict:
        """A finished scheduled run as a new chat, so the person can follow up.

        Only a complete answer becomes context for the chat's next replies.
        """
        cid, mid, aid, timestamp = uid(), uid(), uid(), now()
        status = run["status"] if run["status"] in TERMINAL else "interrupted"
        with self.db:
            self.db.execute(
                "INSERT INTO chats(id,title,custom_title,model,created,updated,web) "
                "VALUES(?,?,1,?,?,?,?)",
                (
                    cid,
                    title.strip()[:160] or "Scheduled run",
                    model,
                    timestamp,
                    timestamp,
                    DEFAULT_WEB,
                ),
            )
            self.db.execute(
                "INSERT INTO messages(id,chat_id,text,created) VALUES(?,?,?,?)",
                (mid, cid, prompt, timestamp),
            )
            self.db.execute("UPDATE chats SET active_leaf=? WHERE id=?", (mid, cid))
            self.db.execute(
                "INSERT INTO attempts(id,chat_id,message_id,request_id,requested_model,"
                "actual_model,thinking,status,content,error,created,updated,web,sources,"
                "searches,reasoning,activity) VALUES(?,?,?,?,?,?,'null',?,?,?,?,?,?,?,?,?,?)",
                (
                    aid,
                    cid,
                    mid,
                    f"run-{run['id']}",
                    run.get("model") or model or "",
                    run.get("actual_model"),
                    status,
                    run.get("content") or "",
                    run.get("error"),
                    timestamp,
                    timestamp,
                    DEFAULT_WEB,
                    json.dumps(run.get("sources") or []),
                    json.dumps(run.get("searches") or []),
                    run.get("reasoning") or "",
                    json.dumps(run.get("activity") or []),
                ),
            )
        return self.chat(cid)

    @attempt_mutation
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
        activity: list | None = None,
        usage: dict | None = None,
    ) -> None:
        with self.db:
            cur = self.db.execute(
                "UPDATE attempts SET content=?,actual_model=COALESCE(?,actual_model),sources=COALESCE(?,sources),searches=COALESCE(?,searches),revision=COALESCE(?,revision+1),reasoning=COALESCE(?,reasoning),reasoning_truncated=COALESCE(?,reasoning_truncated),activity=COALESCE(?,activity),updated=? WHERE id=? AND status='running'",
                (
                    content,
                    actual_model,
                    json.dumps(sources) if sources is not None else None,
                    json.dumps(searches) if searches is not None else None,
                    revision,
                    reasoning,
                    reasoning_truncated,
                    json.dumps(activity) if activity is not None else None,
                    now(),
                    attempt_id,
                ),
            )
            if cur.rowcount != 1:
                raise ValueError("This reply is no longer running.")
            if usage is not None:
                self.db.execute(
                    "UPDATE attempts SET usage=? WHERE id=?",
                    (json.dumps(usage), attempt_id),
                )

    @session_mutation
    def record_shell(self, chat_id: str, command: str, output: str) -> None:
        """Append an explicit user shell result without consuming draft attachments."""
        self._require_idle(chat_id)
        chat = self.chat(chat_id)
        mid, aid, timestamp = uid(), uid(), now()
        with self.db:
            self.db.execute(
                "INSERT INTO messages(id,chat_id,text,created,parent_id) VALUES(?,?,?,?,?)",
                (
                    mid,
                    chat_id,
                    "User executed shell command: " + command,
                    timestamp,
                    chat["active_leaf"],
                ),
            )
            self.db.execute(
                "INSERT INTO attempts(id,chat_id,message_id,request_id,requested_model,thinking,status,content,created,updated,web,mode) VALUES(?,?,?,?,?,'null','complete',?,?,?,'off','normal')",
                (
                    aid,
                    chat_id,
                    mid,
                    uid(),
                    chat["model"] or "",
                    "Untrusted shell output:\n" + output,
                    timestamp,
                    timestamp,
                ),
            )
            self.db.execute(
                "UPDATE chats SET active_leaf=?,updated=? WHERE id=?",
                (mid, timestamp, chat_id),
            )

    @attempt_mutation
    def finish(self, attempt_id: str, status: str, error: str | None = None) -> None:
        if status not in TERMINAL:
            raise ValueError("Invalid reply state.")
        with self.db:
            self.db.execute(
                "UPDATE attempts SET status=?,error=?,updated=? WHERE id=? AND status='running'",
                (status, error, now(), attempt_id),
            )
        chat_id = self._one("SELECT chat_id FROM attempts WHERE id=?", (attempt_id,))[
            "chat_id"
        ]
        if chat_id in self._attempt_locks:
            self.release_chat(chat_id)

    def list_chats(
        self, query: str = "", *, workspace: str | Path | None = None
    ) -> list[dict]:
        """Search titles and all retained branches without interpreting SQL wildcards."""
        clauses, args = [], []
        if query:
            clauses.append(
                "(instr(lower(title),lower(?)) > 0 OR EXISTS (SELECT 1 FROM messages m WHERE m.chat_id=c.id AND instr(lower(m.text),lower(?)) > 0) OR EXISTS (SELECT 1 FROM attempts a WHERE a.chat_id=c.id AND instr(lower(a.content),lower(?)) > 0))"
            )
            args.extend([query] * 3)
        if workspace is not None:
            clauses.append("workspace=?")
            args.append(str(Path(workspace).resolve()))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self._rows(
            "SELECT c.* FROM chats c" + where + " ORDER BY updated DESC,id", tuple(args)
        )
        for row in rows:
            row["thinking"] = json.loads(row["thinking"])
        return rows

    def tree(self, chat_id: str) -> list[dict]:
        chat = self._one("SELECT active_leaf FROM chats WHERE id=?", (chat_id,))
        active = {message["id"] for message in self.chat(chat_id)["messages"]}
        rows = self._rows(
            "SELECT id,parent_id,text,created FROM messages WHERE chat_id=? ORDER BY rowid",
            (chat_id,),
        )
        for row in rows:
            row["active"] = row["id"] in active
            row["leaf"] = row["id"] == chat["active_leaf"]
        return rows

    def _require_idle(self, chat_id: str) -> None:
        if self.db.execute(
            "SELECT 1 FROM attempts WHERE chat_id=? AND status='running'", (chat_id,)
        ).fetchone():
            raise ValueError("Stop this reply before changing its session history.")

    @session_mutation
    def select_branch(self, chat_id: str, message_id: str | None) -> dict:
        self._require_idle(chat_id)
        if message_id is not None:
            self._one(
                "SELECT id FROM messages WHERE id=? AND chat_id=?",
                (message_id, chat_id),
            )
        with self.db:
            self.db.execute(
                "UPDATE chats SET active_leaf=?,context_summary='',summary_through=NULL,updated=? WHERE id=?",
                (message_id, now(), chat_id),
            )
        return self.chat(chat_id)

    @session_mutation
    def set_summary(self, chat_id: str, text: str, through: str | None) -> None:
        if len(text) > 128_000:
            raise ValueError("This context summary is too long.")
        active = self.chat(chat_id)["messages"]
        if through is not None and through not in {m["id"] for m in active}:
            raise ValueError("The summary does not belong to the active branch.")
        with self.db:
            self.db.execute(
                "UPDATE chats SET context_summary=?,summary_through=? WHERE id=?",
                (text, through, chat_id),
            )

    def clear_summary(self, chat_id: str) -> None:
        self.set_summary(chat_id, "", None)

    @attempt_mutation
    def append_steering(self, attempt_id: str, texts: list[str]) -> None:
        attempt = self._one(
            "SELECT message_id,status FROM attempts WHERE id=?", (attempt_id,)
        )
        if attempt["status"] != "running":
            raise ValueError("This reply is no longer running.")
        if not texts or any(
            not isinstance(text, str) or len(text) > 1_000_000 for text in texts
        ):
            raise ValueError("Invalid steering message.")
        addition = "\n\n" + "\n\n".join(
            "User steering during this reply:\n" + text for text in texts
        )
        with self.db:
            self.db.execute(
                "UPDATE messages SET text=text || ? WHERE id=?",
                (addition, attempt["message_id"]),
            )

    @session_mutation
    @file_mutation
    def fork_chat(
        self, chat_id: str, message_id: str | None = None, *, title: str | None = None
    ) -> dict:
        """Copy one ancestor path; source branches and file contents are untouched."""
        self._require_idle(chat_id)
        source = self.chat(chat_id)
        leaf = message_id if message_id is not None else source["active_leaf"]
        all_messages = {
            m["id"]: m
            for m in self._rows("SELECT * FROM messages WHERE chat_id=?", (chat_id,))
        }
        path, seen = [], set()
        current = leaf
        while current is not None:
            if current not in all_messages or current in seen:
                raise ValueError("That branch is no longer available.")
            seen.add(current)
            path.append(all_messages[current])
            current = all_messages[current]["parent_id"]
        path.reverse()
        cid, timestamp = uid(), now()
        files = []
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO chats(id,title,custom_title,folder_id,model,thinking,created,updated,web,mode,workspace,origin_chat_id,origin_message_id,imported) VALUES(?,?,1,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        cid,
                        (title or source["title"]).strip()[:160],
                        source["folder_id"],
                        source["model"],
                        json.dumps(source["thinking"]),
                        timestamp,
                        timestamp,
                        DEFAULT_WEB,
                        source["mode"],
                        source["workspace"],
                        chat_id,
                        leaf,
                        source["imported"],
                    ),
                )
                parent = None
                for message in path:
                    mid = uid()
                    self.db.execute(
                        "INSERT INTO messages(id,chat_id,text,created,parent_id) VALUES(?,?,?,?,?)",
                        (mid, cid, message["text"], message["created"], parent),
                    )
                    for attempt in self._rows(
                        "SELECT * FROM attempts WHERE message_id=? ORDER BY rowid",
                        (message["id"],),
                    ):
                        attempt.update(
                            id=uid(),
                            chat_id=cid,
                            message_id=mid,
                            reasoning="",
                            reasoning_truncated=0,
                        )
                        self._insert_record("attempts", attempt)
                    for attachment in self._rows(
                        "SELECT * FROM attachments WHERE message_id=?", (message["id"],)
                    ):
                        original = attachment["id"]
                        attachment.update(id=uid(), chat_id=cid, message_id=mid)
                        destination = self.files / f"{attachment['id']}.blob"
                        data = (self.files / f"{original}.blob").read_bytes()
                        ensure_space(self.root, len(data) + 50 * 1024**2)
                        with os.fdopen(
                            os.open(
                                destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                            ),
                            "wb",
                        ) as output:
                            files.append(destination)
                            output.write(data)
                            output.flush()
                            os.fsync(output.fileno())
                        self._insert_record("attachments", attachment)
                    parent = mid
                self.db.execute(
                    "UPDATE chats SET active_leaf=? WHERE id=?", (parent, cid)
                )
        except BaseException:
            for file in files:
                file.unlink(missing_ok=True)
            raise
        return self.chat(cid)

    def clone_chat(self, chat_id: str, *, title: str | None = None) -> dict:
        return self.fork_chat(chat_id, title=title)

    def _insert_record(self, table: str, record: dict) -> None:
        # Only internal schemas call this helper; imported keys never become SQL.
        self.db.execute(
            f"INSERT INTO {table}({','.join(record)}) VALUES({','.join('?' for _ in record)})",
            tuple(record.values()),
        )
