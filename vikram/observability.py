from __future__ import annotations

import os

from vikram.logging import get_logger
from vikram.settings import VikramSettings

logger = get_logger(__name__)
_initialized = False


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

    os.environ["OTEL_SERVICE_NAME"] = settings.observability_service_name
    if settings.observability_otlp_endpoint:
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = settings.observability_otlp_endpoint

    from strands.telemetry.config import StrandsTelemetry

    telemetry = StrandsTelemetry()
    if settings.observability_otlp_endpoint:
        telemetry.setup_otlp_exporter(endpoint=settings.observability_otlp_endpoint)
    if not settings.observability_disable_metrics:
        telemetry.setup_meter(
            enable_otlp_exporter=settings.observability_otlp_endpoint is not None
        )
    _initialized = True
    logger.info(
        "observability_initialized",
        service_name=settings.observability_service_name,
        environment=settings.environment,
        otlp_endpoint_configured=settings.observability_otlp_endpoint is not None,
        capture_message_content=capture_message_content,
        disable_metrics=settings.observability_disable_metrics,
        runtime="strands",
    )
    return True


def reset_observability_for_tests() -> None:
    global _initialized
    _initialized = False
