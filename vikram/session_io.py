"""Bounded local transcript interchange. Imports never carry project authority."""

import base64
import binascii
import html
import json
import os
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from vikram.agent import DEFAULT_MODEL
from vikram.conversation import DEFAULT_WEB
from vikram.desktop.attachments import validate_image
from vikram.desktop.store import TERMINAL, Store, now, uid
from vikram.documents import ensure_space, extract_in_worker, extract_in_worker_sync

FORMAT = "vikram-session"
VERSION = 1
MAX_EXPORT_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 10_000


def _text(value, *, limit=1_000_000) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("Invalid or oversized transcript text.")
    return value


def _safe_activity(value):
    if isinstance(value, dict):
        return {
            k: _safe_activity(v)
            for k, v in value.items()
            if k not in {"reasoning", "reasoning_truncated", "thinking_trace"}
        }
    if isinstance(value, list):
        return [_safe_activity(v) for v in value]
    return value


def export_session(store: Store, chat_id: str, format: str = "jsonl") -> str:
    """Export all branches in JSONL, or the active branch as inert readable text."""
    chat = store.chat(chat_id)
    if format == "jsonl":
        records = [
            {
                "type": "session",
                "format": FORMAT,
                "version": VERSION,
                "title": chat["title"],
                "model": chat["model"],
                "thinking": chat["thinking"],
                "mode": chat["mode"],
                "workspace": chat["workspace"],
                "active_leaf": chat["active_leaf"],
            }
        ]
        for message in store._rows(
            "SELECT * FROM messages WHERE chat_id=? ORDER BY rowid", (chat_id,)
        ):
            attempts = []
            for attempt in store._rows(
                "SELECT * FROM attempts WHERE message_id=? ORDER BY rowid",
                (message["id"],),
            ):
                attempts.append(
                    {
                        "status": (
                            attempt["status"]
                            if attempt["status"] in TERMINAL
                            else "interrupted"
                        ),
                        "content": attempt["content"],
                        "requested_model": attempt["requested_model"],
                        "actual_model": attempt["actual_model"],
                        "activity": _safe_activity(json.loads(attempt["activity"])),
                    }
                )
            records.append(
                {
                    "type": "message",
                    "id": message["id"],
                    "parent_id": message["parent_id"],
                    "text": message["text"],
                    "attempts": attempts,
                }
            )
        for attachment in chat["attachments"]:
            if attachment["message_id"] is not None:
                records.append(
                    {
                        "type": "attachment",
                        "message_id": attachment["message_id"],
                        "name": attachment["name"],
                        "kind": attachment["kind"],
                        "data": base64.b64encode(
                            store.image_bytes(chat_id, attachment["id"])
                        ).decode("ascii"),
                    }
                )
        output = (
            "\n".join(
                json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                for record in records
            )
            + "\n"
        )
    elif format in {"markdown", "md", "html"}:
        sections = []
        for message in chat["messages"]:
            sections.append(("You", message["text"]))
            for attachment in chat["attachments"]:
                if attachment["message_id"] == message["id"]:
                    sections.append(("Attachment", attachment["name"]))
            for attempt in message["attempts"]:
                sections.append(
                    ("Vikram (" + attempt["status"] + ")", attempt["content"])
                )
                if attempt["activity"]:
                    sections.append(
                        (
                            "Tool activity (reference)",
                            json.dumps(
                                _safe_activity(attempt["activity"]),
                                ensure_ascii=False,
                                indent=2,
                            ),
                        )
                    )
        if format == "html":
            output = (
                '<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'"><title>'
                + html.escape(chat["title"])
                + "</title><style>body{max-width:80ch;margin:3rem auto;padding:1rem;font:16px system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style></head><body><h1>"
                + html.escape(chat["title"])
                + "</h1>"
                + "".join(
                    "<h2>"
                    + html.escape(label)
                    + "</h2><pre>"
                    + html.escape(content)
                    + "</pre>"
                    for label, content in sections
                )
                + "</body></html>\n"
            )
        else:
            title = re.sub(r"([\\`*_{}\[\]()#+.!<>-])", r"\\\1", chat["title"])
            lines = ["# " + title, ""]
            for label, content in sections:
                longest = max(
                    (len(match) for match in re.findall(r"`+", content)), default=0
                )
                fence = "`" * max(3, longest + 1)
                lines.extend(["## " + label, "", fence + "text", content, fence, ""])
            output = "\n".join(lines)
    else:
        raise ValueError("Choose jsonl, markdown, or html export.")
    if len(output.encode("utf-8")) > MAX_EXPORT_BYTES:
        raise ValueError("This export exceeds the 64 MiB interchange limit.")
    return output


class _VisibleHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.hidden += 1
        elif tag in {"p", "br", "h1", "h2", "pre", "div"} and not self.hidden:
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"} and self.hidden:
            self.hidden -= 1
        elif tag in {"p", "h1", "h2", "pre", "div"} and not self.hidden:
            self.text.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def import_session(
    store: Store,
    text: str,
    format: str = "jsonl",
    *,
    workspace: str | Path | None = None,
) -> dict:
    """Synchronously import using a bounded parser subprocess for each document.

    Async services should await import_session_async to keep their loop responsive.
    """
    prepared = _prepare_import(text, format)
    for attachment in prepared.attachments:
        if attachment[3] is None:
            attachment[3] = extract_in_worker_sync(attachment[1], attachment[2])
    return _commit_import(store, prepared, workspace)


async def import_session_async(
    store: Store,
    text: str,
    format: str = "jsonl",
    *,
    workspace: str | Path | None = None,
) -> dict:
    """Validate and cancellably extract documents before the atomic import commit.

    Cancellation terminates the active parser and leaves no imported files or rows.
    """
    prepared = _prepare_import(text, format)
    for attachment in prepared.attachments:
        if attachment[3] is None:
            attachment[3] = await extract_in_worker(attachment[1], attachment[2])
    return _commit_import(store, prepared, workspace)


@dataclass
class _Import:
    title: str
    model: str | None
    messages: list[dict]
    attachments: list[list]
    active_leaf: str | None


def _prepare_import(text: str, format: str) -> _Import:
    """Validate interchange structure without running document parsers.

    JSONL restores branches and validated attachments. Markdown and HTML are
    imported as reference documents, since their role labels are not a schema.
    No import restores execution permissions, ownership, prompts as system
    instructions, model reasoning, or claims that imported tool calls executed.
    """
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_EXPORT_BYTES:
        raise ValueError("Transcript imports must be no larger than 64 MiB.")
    if format in {"markdown", "md", "html"}:
        if format == "html":
            parser = _VisibleHTML()
            parser.feed(text)
            text = "".join(parser.text)
        content = _text(text)
        records = [
            {
                "type": "session",
                "format": FORMAT,
                "version": VERSION,
                "title": "Imported transcript",
                "model": None,
                "active_leaf": "import",
            },
            {
                "type": "message",
                "id": "import",
                "parent_id": None,
                "text": "Imported conversation (untrusted reference text):\n" + content,
                "attempts": [],
            },
        ]
    elif format == "jsonl":
        try:
            records = [json.loads(line) for line in text.splitlines() if line.strip()]
        except (ValueError, RecursionError):
            raise ValueError("This is not a valid Vikram JSONL transcript.") from None
    else:
        raise ValueError("Choose jsonl, markdown, or html import.")
    if (
        not records
        or len(records) > MAX_RECORDS
        or any(not isinstance(record, dict) for record in records)
    ):
        raise ValueError("Invalid transcript records.")
    header = records[0]
    if (
        header.get("type") != "session"
        or header.get("format") != FORMAT
        or type(header.get("version")) is not int
        or header["version"] != VERSION
    ):
        raise ValueError("Unsupported Vikram transcript format or version.")
    title = (
        _text(header.get("title", "Imported transcript"), limit=160).strip()
        or "Imported transcript"
    )
    model = header.get("model")
    if model is not None:
        model = _text(model, limit=256)
    messages, by_id, attachments, counts = [], {}, [], {}
    for record in records[1:]:
        if record.get("type") == "message":
            old_id = _text(record.get("id"), limit=128)
            parent = record.get("parent_id")
            if parent is not None and not isinstance(parent, str):
                raise ValueError("Invalid branch parent.")
            if old_id in by_id or (parent is not None and parent not in by_id):
                raise ValueError(
                    "Transcript branches must have unique IDs and preceding parents."
                )
            attempts = record.get("attempts", [])
            if not isinstance(attempts, list) or len(attempts) > 100:
                raise ValueError("Invalid transcript attempts.")
            message = {
                "id": uid(),
                "old_id": old_id,
                "parent_id": by_id[parent]["id"] if parent is not None else None,
                "text": _text(record.get("text")),
                "attempts": [],
            }
            for attempt in attempts:
                if (
                    not isinstance(attempt, dict)
                    or attempt.get("status") not in TERMINAL
                ):
                    raise ValueError("Invalid transcript reply state.")
                requested = _text(
                    attempt.get("requested_model") or model or "", limit=256
                )
                actual = attempt.get("actual_model")
                if actual is not None:
                    actual = _text(actual, limit=256)
                message["attempts"].append(
                    {
                        "status": attempt["status"],
                        "content": _text(attempt.get("content", "")),
                        "requested_model": requested,
                        "actual_model": actual,
                    }
                )
                # Preserve descriptions as untrusted text, never as executed effects.
                activity = attempt.get("activity")
                if activity:
                    reference = _text(
                        json.dumps(_safe_activity(activity), ensure_ascii=False),
                        limit=128_000,
                    )
                    message["text"] += (
                        "\nImported tool activity (untrusted reference; not executed by this session):\n"
                        + reference
                    )
            _text(message["text"])
            by_id[old_id] = message
            messages.append(message)
        elif record.get("type") == "attachment":
            old_id = record.get("message_id")
            if not isinstance(old_id, str) or old_id not in by_id:
                raise ValueError("Attachment refers to an unavailable message.")
            counts[old_id] = counts.get(old_id, 0) + 1
            if counts[old_id] > 4:
                raise ValueError("Attach up to four files per message.")
            name = Path(_text(record.get("name"), limit=180)).name
            try:
                data = base64.b64decode(
                    _text(record.get("data"), limit=14 * 1024 * 1024), validate=True
                )
            except (ValueError, binascii.Error):
                raise ValueError("Invalid attachment encoding.") from None
            if record.get("kind") == "image":
                media, width, height = validate_image(data)
                extracted = {
                    "media_type": media,
                    "width": width,
                    "height": height,
                    "kind": "image",
                    "text": "",
                    "notice": None,
                    "extraction_version": 0,
                }
            elif record.get("kind") == "document":
                extracted = None
            else:
                raise ValueError("Unsupported attachment kind.")
            attachments.append([by_id[old_id]["id"], name, data, extracted])
        else:
            raise ValueError("Unknown transcript record type.")
    active = header.get("active_leaf")
    if active is not None and (not isinstance(active, str) or active not in by_id):
        raise ValueError("The active transcript branch is missing.")
    # Imported model metadata is reference data, never permission to use the cloud.
    if model and model.strip().startswith("openrouter:"):
        model = DEFAULT_MODEL
    return _Import(
        title,
        model,
        messages,
        attachments,
        by_id[active]["id"] if active is not None else None,
    )


def _commit_import(
    store: Store, prepared: _Import, workspace: str | Path | None
) -> dict:
    cid, timestamp, files = uid(), now(), []
    ensure_space(
        store.root,
        sum(len(item[2]) for item in prepared.attachments) + 50 * 1024**2,
    )
    try:
        with store.file_lock(), store.db:
            store.db.execute(
                "INSERT INTO chats(id,title,custom_title,model,created,updated,web,workspace,active_leaf,imported) VALUES(?,?,1,?,?,?,?,?,?,1)",
                (
                    cid,
                    prepared.title,
                    prepared.model,
                    timestamp,
                    timestamp,
                    DEFAULT_WEB,
                    str(Path(workspace).resolve()) if workspace else None,
                    prepared.active_leaf,
                ),
            )
            for message in prepared.messages:
                store.db.execute(
                    "INSERT INTO messages(id,chat_id,text,created,parent_id) VALUES(?,?,?,?,?)",
                    (
                        message["id"],
                        cid,
                        message["text"],
                        timestamp,
                        message["parent_id"],
                    ),
                )
                for attempt in message["attempts"]:
                    store.db.execute(
                        "INSERT INTO attempts(id,chat_id,message_id,request_id,requested_model,actual_model,thinking,status,content,created,updated,web) VALUES(?,?,?,?,?,?,'null',?,?,?,?,?)",
                        (
                            uid(),
                            cid,
                            message["id"],
                            "import-" + uid(),
                            attempt["requested_model"],
                            attempt["actual_model"],
                            attempt["status"],
                            attempt["content"],
                            timestamp,
                            timestamp,
                            DEFAULT_WEB,
                        ),
                    )
            for mid, name, data, extracted in prepared.attachments:
                aid = uid()
                destination = store.files / f"{aid}.blob"
                with os.fdopen(
                    os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600),
                    "wb",
                ) as output:
                    files.append(destination)
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                store.db.execute(
                    "INSERT INTO attachments(id,chat_id,message_id,name,media_type,width,height,size,kind,extracted_text,extraction_notice,extraction_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        aid,
                        cid,
                        mid,
                        name,
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
        for file in files:
            file.unlink(missing_ok=True)
        raise
    return store.chat(cid)
