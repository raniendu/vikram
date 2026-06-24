from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from cloudevents.v1.http.event import CloudEvent

from vikram.agent import build_agent
from vikram.logging import get_logger, safe_metadata, thread_hash
from vikram.settings import VikramSettings
from vikram.spec import ensure_surface_allowed, load_spec

logger = get_logger(__name__)
RUNTIME_HISTORY_VERSION = "strands-v1"


@dataclass(frozen=True)
class ThreadRecord:
    interface: str
    external_thread_id: str
    agent_name: str
    message_history_json: bytes | None


@dataclass(frozen=True)
class InboundMessage:
    interface: str
    external_thread_id: str
    prompt: str
    agent_name: str | None
    default_agent: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConversationReply:
    interface: str
    external_thread_id: str
    agent_name: str
    output: str


@dataclass(frozen=True)
class EnqueuedEvent:
    workflow_id: str
    status: str


class RunnableAgent(Protocol):
    async def run(
        self,
        user_prompt: str,
        *,
        message_history: list[Any],
        conversation_id: str,
    ) -> Any: ...


AgentFactory = Callable[[str], RunnableAgent]
MessageEnqueuer = Callable[[InboundMessage], Awaitable[EnqueuedEvent]]


class ThreadStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS threads (
                    interface TEXT NOT NULL,
                    external_thread_id TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    message_history_json BLOB,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (interface, external_thread_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_updates (
                    bot_name TEXT NOT NULL,
                    update_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (bot_name, update_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(telegram_updates)")
            }
            if columns == {"update_id", "created_at"}:
                conn.execute(
                    "ALTER TABLE telegram_updates RENAME TO telegram_updates_legacy"
                )
                conn.execute(
                    """
                    CREATE TABLE telegram_updates (
                        bot_name TEXT NOT NULL,
                        update_id INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (bot_name, update_id)
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO telegram_updates (
                        bot_name,
                        update_id,
                        created_at
                    )
                    SELECT 'telegram', update_id, created_at
                    FROM telegram_updates_legacy
                    """
                )
                conn.execute("DROP TABLE telegram_updates_legacy")
            self._ensure_runtime_history_version(conn)

    def _ensure_runtime_history_version(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            """
            SELECT value
            FROM runtime_metadata
            WHERE key = 'history_runtime'
            """
        ).fetchone()
        if row is not None and row["value"] == RUNTIME_HISTORY_VERSION:
            return
        conn.execute("UPDATE threads SET message_history_json = NULL")
        conn.execute(
            """
            INSERT INTO runtime_metadata (key, value, updated_at)
            VALUES ('history_runtime', ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (RUNTIME_HISTORY_VERSION, _utc_now()),
        )

    def get_thread(
        self,
        interface: str,
        external_thread_id: str,
        *,
        default_agent: str,
    ) -> ThreadRecord:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT interface, external_thread_id, agent_name, message_history_json
                FROM threads
                WHERE interface = ? AND external_thread_id = ?
                """,
                (interface, external_thread_id),
            ).fetchone()
        if row is None:
            return ThreadRecord(interface, external_thread_id, default_agent, None)
        history = row["message_history_json"]
        return ThreadRecord(
            interface=row["interface"],
            external_thread_id=row["external_thread_id"],
            agent_name=row["agent_name"],
            message_history_json=bytes(history) if history is not None else None,
        )

    def set_history(
        self,
        interface: str,
        external_thread_id: str,
        *,
        agent_name: str,
        message_history_json: bytes,
    ) -> None:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO threads (
                    interface,
                    external_thread_id,
                    agent_name,
                    message_history_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(interface, external_thread_id) DO UPDATE SET
                    agent_name = excluded.agent_name,
                    message_history_json = excluded.message_history_json,
                    updated_at = excluded.updated_at
                """,
                (
                    interface,
                    external_thread_id,
                    agent_name,
                    message_history_json,
                    now,
                    now,
                ),
            )

    def set_agent(
        self, interface: str, external_thread_id: str, agent_name: str
    ) -> None:
        thread = self.get_thread(
            interface, external_thread_id, default_agent=agent_name
        )
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO threads (
                    interface,
                    external_thread_id,
                    agent_name,
                    message_history_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(interface, external_thread_id) DO UPDATE SET
                    agent_name = excluded.agent_name,
                    updated_at = excluded.updated_at
                """,
                (
                    interface,
                    external_thread_id,
                    agent_name,
                    thread.message_history_json,
                    now,
                    now,
                ),
            )

    def reset_history(self, interface: str, external_thread_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE threads
                SET message_history_json = NULL, updated_at = ?
                WHERE interface = ? AND external_thread_id = ?
                """,
                (_utc_now(), interface, external_thread_id),
            )

    def claim_telegram_update(
        self, bot_name: str | int, update_id: int | None = None
    ) -> bool:
        if update_id is None:
            update_id = int(bot_name)
            bot_name = "telegram"
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO telegram_updates (
                    bot_name,
                    update_id,
                    created_at
                )
                VALUES (?, ?, ?)
                """,
                (str(bot_name), update_id, _utc_now()),
            )
        return cursor.rowcount == 1


class ConversationService:
    def __init__(
        self,
        *,
        settings: VikramSettings,
        store: ThreadStore,
        agent_factory: AgentFactory | None = None,
    ):
        self.settings = settings
        self.store = store
        self._agent_factory = agent_factory
        self._agent_cache: dict[str, RunnableAgent] = {}

    async def send_message(self, message: InboundMessage) -> ConversationReply:
        thread = self.store.get_thread(
            message.interface,
            message.external_thread_id,
            default_agent=message.default_agent or self.settings.default_agent,
        )
        agent_name = message.agent_name or thread.agent_name
        history = _load_history(thread.message_history_json)
        agent = self._get_agent(agent_name)
        conversation_id = f"{message.interface}:{message.external_thread_id}"
        log = logger.bind(
            interface=message.interface,
            thread_hash=thread_hash(message.interface, message.external_thread_id),
            agent=agent_name,
            prompt_length=len(message.prompt),
            history_count=len(history),
            **safe_metadata(message.metadata),
        )
        start = time.perf_counter()
        log.info("agent_run_started")
        try:
            result = await agent.run(
                message.prompt,
                message_history=history,
                conversation_id=conversation_id,
            )
        except Exception:
            log.exception(
                "agent_run_failed",
                duration_ms=round((time.perf_counter() - start) * 1000, 2),
            )
            raise
        log.info(
            "agent_run_succeeded",
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
            output_length=len(str(result.output)),
        )
        self.store.set_history(
            message.interface,
            message.external_thread_id,
            agent_name=agent_name,
            message_history_json=_messages_json(result),
        )
        log.info("thread_history_persisted")
        output = str(result.output)
        context_warning = _context_usage_warning(result, self.settings)
        if context_warning is not None:
            output = f"{output}\n\n{context_warning}"
            log.warning("context_usage_warning_emitted")
        return ConversationReply(
            interface=message.interface,
            external_thread_id=message.external_thread_id,
            agent_name=agent_name,
            output=output,
        )

    def _get_agent(self, name: str) -> RunnableAgent:
        if self._agent_factory is not None:
            return self._agent_factory(name)
        if name not in self._agent_cache:
            spec = load_spec(name, self.settings.spec_root)
            ensure_surface_allowed(spec, "threaded")
            self._agent_cache[name] = build_agent(
                spec=spec, settings=self.settings, surface="threaded"
            )
        return self._agent_cache[name]


def make_message_received_event(message: InboundMessage) -> CloudEvent:
    return CloudEvent(
        {
            "type": "vikram.message.received",
            "source": f"/interfaces/{message.interface}/threads/{message.external_thread_id}",
            "subject": message.external_thread_id,
            "datacontenttype": "application/json",
        },
        {
            "interface": message.interface,
            "external_thread_id": message.external_thread_id,
            "prompt": message.prompt,
            "agent_name": message.agent_name,
            "default_agent": message.default_agent,
            "metadata": message.metadata,
        },
    )


def make_reply_requested_event(
    message: InboundMessage, reply: ConversationReply
) -> CloudEvent:
    return CloudEvent(
        {
            "type": "vikram.message.reply_requested",
            "source": f"/interfaces/{reply.interface}/threads/{reply.external_thread_id}",
            "subject": reply.external_thread_id,
            "datacontenttype": "application/json",
        },
        {
            "interface": reply.interface,
            "external_thread_id": reply.external_thread_id,
            "agent_name": reply.agent_name,
            "output": reply.output,
            "metadata": message.metadata,
        },
    )


def cloud_event_to_dict(event: CloudEvent) -> dict[str, Any]:
    return {
        "attributes": dict(event.get_attributes()),
        "data": event.get_data(),
    }


def cloud_event_from_dict(value: dict[str, Any]) -> CloudEvent:
    return CloudEvent(value["attributes"], value.get("data"))


def inbound_message_from_event(event: CloudEvent) -> InboundMessage:
    data = event.get_data()
    if not isinstance(data, dict):
        raise ValueError("CloudEvent data must be an object")
    return InboundMessage(
        interface=str(data["interface"]),
        external_thread_id=str(data["external_thread_id"]),
        prompt=str(data["prompt"]),
        agent_name=data.get("agent_name"),
        default_agent=data.get("default_agent"),
        metadata=dict(data.get("metadata") or {}),
    )


def _load_history(message_history_json: bytes | None) -> list[Any]:
    if not message_history_json:
        return []
    try:
        value = json.loads(message_history_json.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("thread_history_unreadable_after_strands_cutover")
        return []
    return value if isinstance(value, list) else []


def _messages_json(result: Any) -> bytes:
    all_messages_json = getattr(result, "all_messages_json", None)
    if callable(all_messages_json):
        return all_messages_json()
    all_messages = getattr(result, "all_messages", None)
    if callable(all_messages):
        messages = all_messages()
    else:
        messages = getattr(result, "messages", [])
    return json.dumps(messages, default=str).encode("utf-8")


def _context_usage_warning(result: Any, settings: VikramSettings) -> str | None:
    context_window = settings.context_window_tokens
    warning_ratio = settings.context_warning_ratio
    if context_window <= 0 or warning_ratio <= 0:
        return None
    usage_fn = getattr(result, "usage", None)
    if not callable(usage_fn):
        return None
    try:
        usage = usage_fn()
    except Exception:
        logger.exception("context_usage_unavailable")
        return None
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    if input_tokens <= 0:
        return None
    ratio = input_tokens / context_window
    if ratio < warning_ratio:
        return None
    percent = round(ratio * 100)
    return (
        f"Context warning: this thread is using about {percent}% of the model "
        f"context ({input_tokens:,}/{context_window:,} input tokens). Use /reset "
        "when you are ready to start fresh."
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
