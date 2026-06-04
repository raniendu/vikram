from __future__ import annotations

import logging

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

    logging.getLogger("openlit").setLevel(logging.WARNING)
    import openlit

    capture_message_content = (
        settings.observability_capture_message_content
        and settings.environment.lower() != "production"
    )
    if settings.observability_capture_message_content and not capture_message_content:
        logger.warning("observability_message_content_capture_forced_off")

    openlit.init(
        application_name=settings.observability_service_name,
        service_name=settings.observability_service_name,
        environment=settings.environment,
        otlp_endpoint=settings.observability_otlp_endpoint,
        capture_message_content=capture_message_content,
        disabled_instrumentors=settings.observability_disabled_instrumentor_list,
        disable_metrics=settings.observability_disable_metrics,
    )
    _initialized = True
    logger.info(
        "observability_initialized",
        service_name=settings.observability_service_name,
        environment=settings.environment,
        otlp_endpoint_configured=settings.observability_otlp_endpoint is not None,
        capture_message_content=capture_message_content,
        disable_metrics=settings.observability_disable_metrics,
    )
    return True


def reset_observability_for_tests() -> None:
    global _initialized
    _initialized = False
