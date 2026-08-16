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


def test_trace_context_survives_the_queue_boundary():
    """A webhook and the workflow that answers it must share one trace.

    The inbound request and the DBOS workflow run in different tasks, so the
    only link between them is the traceparent carried on the CloudEvent.
    """
    exporter, tracer = _recording_tracer()

    from vikram.gateway import (
        InboundMessage,
        cloud_event_from_dict,
        cloud_event_to_dict,
        make_message_received_event,
    )
    from vikram.observability import extract_trace_context

    message = InboundMessage(
        interface="telegram",
        external_thread_id="42",
        prompt="hi",
        agent_name=None,
    )

    with tracer.start_as_current_span("POST /telegram/webhook"):
        wire = cloud_event_to_dict(make_message_received_event(message))

    assert "traceparent" in wire["attributes"]

    parent = extract_trace_context(cloud_event_from_dict(wire).get_attributes())
    with tracer.start_as_current_span("vikram.process_inbound_message", context=parent):
        pass

    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    assert len({span.context.trace_id for span in spans}) == 1


def test_extract_trace_context_tolerates_events_without_a_trace():
    """Events queued before tracing was enabled must still process."""
    from vikram.observability import extract_trace_context

    assert extract_trace_context(None) is None
    assert extract_trace_context({}) is None
    assert extract_trace_context({"type": "vikram.message.received"}) is None
    # A non-string attribute value must not blow up the propagator.
    assert extract_trace_context({"traceparent": 1234}) is None


def test_inject_trace_context_is_empty_without_an_active_span():
    """With no span in scope there is nothing to propagate, and that is fine."""
    from vikram.observability import inject_trace_context

    assert inject_trace_context() == {}


def _recording_tracer():
    """Build a tracer that records spans in memory.

    The provider is deliberately *not* installed globally: OpenTelemetry only
    honours ``set_tracer_provider`` once per process, so a global install would
    make these tests depend on order. Propagation reads the active span from the
    context rather than the provider, so a local tracer exercises the real path.
    """
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")
