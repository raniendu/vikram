from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import structlog

try:
    from opentelemetry import trace
except ImportError:  # pragma: no cover - openlit depends on opentelemetry.
    trace = None  # type: ignore[assignment]


def configure_logging(log_level: str = "INFO") -> None:
    logging.basicConfig(
        format="%(message)s",
        level=_normalize_log_level(log_level),
        stream=sys.stdout,
        force=True,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            add_trace_context,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            _normalize_log_level(log_level)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def thread_hash(interface: str, external_thread_id: str) -> str:
    return _hash_value(f"{interface}:{external_thread_id}")


def chat_hash(chat_id: int) -> str:
    return _hash_value(str(chat_id))


def safe_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    metadata = metadata if isinstance(metadata, Mapping) else {}
    return {
        "update_id": metadata.get("update_id"),
        "chat_type": metadata.get("chat_type"),
        "has_from_id": metadata.get("from_id") is not None,
    }


def safe_database_url(database_url: str) -> str:
    parts = urlsplit(database_url)
    if parts.password is None:
        return database_url
    username = parts.username or ""
    hostname = parts.hostname or ""
    port = f":{parts.port}" if parts.port is not None else ""
    netloc = f"{username}:***@{hostname}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def add_trace_context(
    logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    if trace is None:
        return event_dict
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        event_dict["trace_id"] = f"{span_context.trace_id:032x}"
        event_dict["span_id"] = f"{span_context.span_id:016x}"
    return event_dict


def _hash_value(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:12]


def _normalize_log_level(log_level: str) -> int:
    level = getattr(logging, log_level.strip().upper(), None)
    return level if isinstance(level, int) else logging.INFO
