from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.context import Context

from vikram.logging import get_logger
from vikram.settings import VikramSettings

logger = get_logger(__name__)
_initialized = False

TRACER_NAME = "vikram"
# W3C Trace Context headers, which are also the CloudEvents distributed-tracing
# extension attribute names.
_TRACE_HEADERS = ("traceparent", "tracestate")


def get_tracer() -> trace.Tracer:
    """Return Vikram's tracer.

    Safe to call whether or not :func:`init_observability` ran: with no SDK
    configured OpenTelemetry hands back a no-op tracer, so spans cost almost
    nothing and callers never need to branch on whether tracing is enabled.
    OpenLIT installs the real SDK when tracing is turned on.
    """
    return trace.get_tracer(TRACER_NAME)


def record_span_exception(span: trace.Span, exc: BaseException) -> None:
    """Mark ``span`` as failed because of ``exc``.

    Only the exception type reaches the span status message; messages can carry
    prompt text or credentials.
    """
    span.record_exception(exc)
    span.set_status(trace.Status(trace.StatusCode.ERROR, type(exc).__name__))


def set_span_attributes(span: trace.Span, attributes: dict[str, Any]) -> None:
    """Set every non-``None`` attribute on ``span``."""
    for key, value in attributes.items():
        if value is not None:
            span.set_attribute(key, value)


def inject_trace_context() -> dict[str, str]:
    """Serialize the active trace context as W3C ``traceparent``/``tracestate``.

    Used to carry a trace across the DBOS queue boundary: a Telegram webhook and
    the workflow that answers it run in different tasks (often different
    processes), so without this the inbound request and the reply look like two
    unrelated traces. The keys match the CloudEvents distributed-tracing
    extension, so they ride along as ordinary event attributes.

    Returns an empty mapping when no span is active or tracing is not
    configured.
    """
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


def extract_trace_context(carrier: Mapping[str, Any] | None) -> Context | None:
    """Rebuild a parent context from ``carrier``, or ``None`` if it has none.

    Tolerates events queued before tracing was enabled, and events whose
    attributes carry non-string values, by simply finding no trace headers.
    """
    if not carrier:
        return None
    headers = {
        key: value
        for key, value in carrier.items()
        if key in _TRACE_HEADERS and isinstance(value, str)
    }
    if not headers:
        return None
    return propagate.extract(headers)


def init_observability(settings: VikramSettings) -> bool:
    global _initialized
    if not settings.observability_enabled:
        logger.info("observability_disabled")
        return False
    if _initialized:
        logger.info("observability_already_initialized")
        return False

    capture_message_content = (
        settings.observability_capture_message_content
        and settings.environment.lower() != "production"
    )
    if settings.observability_capture_message_content and not capture_message_content:
        logger.warning("observability_message_content_capture_forced_off")

    logging.getLogger("openlit").setLevel(logging.WARNING)
    import openlit

    openlit.init(
        application_name=settings.observability_service_name,
        service_name=settings.observability_service_name,
        environment=settings.environment,
        otlp_endpoint=settings.observability_otlp_endpoint,
        capture_message_content=capture_message_content,
        disabled_instrumentors=settings.observability_disabled_instrumentor_list,
        disable_metrics=settings.observability_disable_metrics,
    )
    from pydantic_ai import Agent
    from pydantic_ai.models.instrumented import InstrumentationSettings

    Agent.instrument_all(
        InstrumentationSettings(include_content=capture_message_content)
    )
    _initialized = True
    logger.info(
        "observability_initialized",
        service_name=settings.observability_service_name,
        environment=settings.environment,
        otlp_endpoint_configured=settings.observability_otlp_endpoint is not None,
        capture_message_content=capture_message_content,
        disable_metrics=settings.observability_disable_metrics,
        runtime="pydantic-ai",
    )
    return True


def reset_observability_for_tests() -> None:
    global _initialized
    _initialized = False
