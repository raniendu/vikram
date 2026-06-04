import logging
import sys
from types import SimpleNamespace

from vikram.observability import init_observability, reset_observability_for_tests
from vikram.settings import VikramSettings


def teardown_function():
    reset_observability_for_tests()
    logging.getLogger("openlit").setLevel(logging.NOTSET)


def test_observability_is_disabled_by_default(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "openlit", SimpleNamespace(init=calls.append))

    enabled = init_observability(VikramSettings(_env_file=None))

    assert enabled is False
    assert calls == []


def test_observability_initializes_openlit_with_safe_defaults(monkeypatch):
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "openlit",
        SimpleNamespace(init=lambda **kwargs: calls.append(kwargs)),
    )

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


def test_observability_is_idempotent(monkeypatch):
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "openlit",
        SimpleNamespace(init=lambda **kwargs: calls.append(kwargs)),
    )
    settings = VikramSettings(_env_file=None, VIKRAM_OBSERVABILITY_ENABLED=True)

    assert init_observability(settings) is True
    assert init_observability(settings) is False
    assert len(calls) == 1


def test_observability_never_captures_message_content_in_production(monkeypatch):
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "openlit",
        SimpleNamespace(init=lambda **kwargs: calls.append(kwargs)),
    )

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
