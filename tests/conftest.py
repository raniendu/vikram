from __future__ import annotations

import io
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
import structlog


@pytest.fixture(autouse=True)
def reset_structlog_configuration() -> Iterator[None]:
    """Undo any global structlog configuration a test performed.

    ``configure_logging`` installs a level-filtering wrapper class process-wide.
    Without this reset, a test that runs the CLI (which configures logging at
    WARNING) would silently suppress the info-level events a later test asserts
    on, making failures depend on test order.
    """
    try:
        yield
    finally:
        structlog.reset_defaults()


@pytest.fixture
def log_events() -> Iterator[list[dict[str, Any]]]:
    """Capture structlog events emitted during a test.

    Vikram logs through structlog, which only routes into stdlib logging once
    ``configure_logging`` has run. ``caplog`` therefore sees nothing in a plain
    unit test. ``structlog.testing.capture_logs`` intercepts at the structlog
    layer instead, so assertions hold regardless of logging configuration.

    Each captured entry is a dict of the bound key/values plus ``event`` (the
    event name) and ``log_level``.
    """
    with structlog.testing.capture_logs() as events:
        yield events


@contextmanager
def captured_json_logs(level: str = "INFO") -> Iterator[list[dict[str, Any]]]:
    """Capture the JSON log lines Vikram's *configured* pipeline emits.

    Unlike :func:`log_events`, this exercises the real processor chain, so it
    sees everything that chain adds — notably the context variables merged by
    ``merge_contextvars``, which ``structlog.testing.capture_logs`` bypasses.
    That makes it the only way to assert on request correlation.

    The yielded list is filled when the block exits, so assert after the
    ``with``. Non-JSON lines (third-party libraries logging plain text) are
    skipped.
    """
    from vikram.logging import configure_logging

    buffer = io.StringIO()
    events: list[dict[str, Any]] = []
    configure_logging(level, stream=buffer)
    try:
        yield events
    finally:
        # Put stdlib logging back on stdout so a dead buffer cannot swallow
        # output from later tests.
        configure_logging(level, stream=sys.stdout)
        for line in buffer.getvalue().splitlines():
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict) and "event" in parsed:
                events.append(parsed)


def log_event_names(events: list[dict[str, Any]]) -> list[str]:
    return [entry["event"] for entry in events]


def find_log_event(events: list[dict[str, Any]], name: str) -> dict[str, Any]:
    """Return the single captured event called ``name``, failing loudly if absent."""
    matches = [entry for entry in events if entry.get("event") == name]
    if not matches:
        raise AssertionError(
            f"No {name!r} log event. Captured: {log_event_names(events)}"
        )
    if len(matches) > 1:
        raise AssertionError(f"Expected one {name!r} log event, got {len(matches)}.")
    return matches[0]


# --- pytest --evals ---------------------------------------------------------
# Opt-in agent quality gate (evals/gate.py). Plain `pytest` never touches it,
# so the default suite stays offline and fast, as CI runs it.


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("vikram evals")
    group.addoption(
        "--evals",
        action="store_true",
        default=False,
        help="After the tests pass, score the agents on the working tree with "
        "the eval suite (needs Ollama) and fail if any case got worse.",
    )
    group.addoption(
        "--eval-agent",
        action="append",
        default=None,
        help="Agent to score (repeatable). Default: every agent with cases.",
    )
    group.addoption(
        "--eval-repeats",
        type=int,
        default=None,
        help="Repeats per case (default: VIKRAM_EVALS_REPEATS or 3).",
    )


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    config = session.config
    if not config.getoption("--evals"):
        return
    reporter = config.pluginmanager.get_plugin("terminalreporter")

    def say(line: str) -> None:
        if reporter is not None:
            reporter.write_line(line)

    if exitstatus != 0:
        say("pytest --evals: tests failed, so the evals were skipped.")
        return

    import os
    from contextlib import nullcontext

    from evals.gate import run_gate
    from vikram.logging import configure_logging

    configure_logging(
        os.environ.get("VIKRAM_EVALS_LOG_LEVEL", "WARNING"), stream=sys.stderr
    )
    say("")
    capture = config.pluginmanager.get_plugin("capturemanager")
    # Show the progress lines live instead of buffering them.
    with capture.global_and_fixture_disabled() if capture else nullcontext():
        outcome = run_gate(
            config.rootpath,
            agents=config.getoption("--eval-agent"),
            repeats=config.getoption("--eval-repeats"),
        )
    say("")
    for line in outcome.report().splitlines():
        say(line)
    if outcome.failed:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
