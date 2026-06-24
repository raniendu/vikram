import os
import sys
from types import SimpleNamespace

from vikram.observability import init_observability, reset_observability_for_tests
from vikram.settings import VikramSettings


def teardown_function():
    reset_observability_for_tests()


def test_observability_is_disabled_by_default(monkeypatch):
    calls = []
    _install_fake_strands_telemetry(monkeypatch, calls)

    enabled = init_observability(VikramSettings(_env_file=None))

    assert enabled is False
    assert calls == []


def test_observability_initializes_strands_telemetry(monkeypatch):
    calls = []
    _install_fake_strands_telemetry(monkeypatch, calls)

    enabled = init_observability(
        VikramSettings(
            _env_file=None,
            ENVIRONMENT="local",
            VIKRAM_OBSERVABILITY_ENABLED=True,
            VIKRAM_OTLP_ENDPOINT="http://jaeger:4318",
        )
    )

    assert enabled is True
    assert calls == [
        ("init", {}),
        ("setup_otlp_exporter", {"endpoint": "http://jaeger:4318"}),
        ("setup_meter", {"enable_otlp_exporter": True}),
    ]
    assert os.environ["OTEL_SERVICE_NAME"] == "vikram"
    assert os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://jaeger:4318"


def test_observability_is_idempotent(monkeypatch):
    calls = []
    _install_fake_strands_telemetry(monkeypatch, calls)
    settings = VikramSettings(_env_file=None, VIKRAM_OBSERVABILITY_ENABLED=True)

    assert init_observability(settings) is True
    assert init_observability(settings) is False
    assert [call[0] for call in calls].count("init") == 1


def test_observability_never_captures_message_content_in_production(monkeypatch):
    calls = []
    _install_fake_strands_telemetry(monkeypatch, calls)

    enabled = init_observability(
        VikramSettings(
            _env_file=None,
            ENVIRONMENT="production",
            VIKRAM_OBSERVABILITY_ENABLED=True,
            VIKRAM_OBSERVABILITY_CAPTURE_MESSAGE_CONTENT=True,
        )
    )

    assert enabled is True
    assert calls[0][0] == "init"


def _install_fake_strands_telemetry(monkeypatch, calls):
    class FakeTelemetry:
        def __init__(self):
            calls.append(("init", {}))

        def setup_otlp_exporter(self, **kwargs):
            calls.append(("setup_otlp_exporter", kwargs))
            return self

        def setup_meter(self, **kwargs):
            calls.append(("setup_meter", kwargs))
            return self

    monkeypatch.setitem(
        sys.modules,
        "strands.telemetry.config",
        SimpleNamespace(StrandsTelemetry=FakeTelemetry),
    )
