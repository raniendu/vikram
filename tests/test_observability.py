import logging
import sys
from types import SimpleNamespace

from vikram.observability import init_observability, reset_observability_for_tests
from vikram.settings import VikramSettings


def teardown_function():
    reset_observability_for_tests()
    logging.getLogger("openlit").setLevel(logging.NOTSET)


def _install_fake_openlit(monkeypatch, calls, instrumentation_calls=None):
    monkeypatch.setitem(
        sys.modules,
        "openlit",
        SimpleNamespace(init=lambda **kwargs: calls.append(kwargs)),
    )
    monkeypatch.setattr(
        "pydantic_ai.Agent.instrument_all",
        lambda settings: (
            instrumentation_calls.append(settings)
            if instrumentation_calls is not None
            else None
        ),
    )


def test_observability_is_disabled_by_default(monkeypatch):
    calls = []
    _install_fake_openlit(monkeypatch, calls)

    enabled = init_observability(VikramSettings(_env_file=None))

    assert enabled is False
    assert calls == []


def test_observability_initializes_openlit_for_pydantic_ai(monkeypatch):
    calls = []
    instrumentation_calls = []
    _install_fake_openlit(monkeypatch, calls, instrumentation_calls)

    enabled = init_observability(
        VikramSettings(
            _env_file=None,
            ENVIRONMENT="local",
            VIKRAM_OBSERVABILITY_ENABLED=True,
            VIKRAM_OTLP_ENDPOINT="http://jaeger:4318",
        )
    )

    assert enabled is True
    assert logging.getLogger("openlit").level == logging.WARNING
    assert calls == [
        {
            "application_name": "vikram",
            "service_name": "vikram",
            "environment": "local",
            "otlp_endpoint": "http://jaeger:4318",
            "capture_message_content": False,
            "disabled_instrumentors": ["mistral"],
            "disable_metrics": False,
        }
    ]
    assert len(instrumentation_calls) == 1
    assert instrumentation_calls[0].include_content is False


def test_observability_is_idempotent(monkeypatch):
    calls = []
    _install_fake_openlit(monkeypatch, calls)
    settings = VikramSettings(_env_file=None, VIKRAM_OBSERVABILITY_ENABLED=True)

    assert init_observability(settings) is True
    assert init_observability(settings) is False
    assert len(calls) == 1


def test_observability_never_captures_message_content_in_production(monkeypatch):
    calls = []
    _install_fake_openlit(monkeypatch, calls)

    enabled = init_observability(
        VikramSettings(
            _env_file=None,
            ENVIRONMENT="production",
            VIKRAM_OBSERVABILITY_ENABLED=True,
            VIKRAM_OBSERVABILITY_CAPTURE_MESSAGE_CONTENT=True,
        )
    )

    assert enabled is True
    assert calls[0]["capture_message_content"] is False
