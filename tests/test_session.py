from __future__ import annotations

import asyncio
import json

import pytest

from vikram.agent import ApprovalRequest
from vikram.events import Event
from vikram.session import Session, SessionError, sse_stream
from vikram.session_worker import SessionWorker

# --- worker: approvals -------------------------------------------------


class _CapturingWorker(SessionWorker):
    """A worker whose emitted events are captured instead of written."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.emitted: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.emitted.append(event)


def _worker(tmp_path, **kw):
    return _CapturingWorker(session_id="s1", agent_id="coder", workspace=tmp_path, **kw)


def _request(tool_name="write_file", call_id="c1"):
    return ApprovalRequest(
        tool_name=tool_name, tool_call_id=call_id, args={"path": "a.txt"}
    )


async def test_approval_round_trips_a_decision(tmp_path):
    worker = _worker(tmp_path)
    task = asyncio.create_task(worker._ask_approval(_request()))
    await asyncio.sleep(0)

    requested = worker.emitted[0]
    assert requested.type == "approval.requested"
    assert requested.payload["tool_name"] == "write_file"
    assert requested.payload["input"] == {"path": "a.txt"}
    assert requested.payload["auto"] is False

    worker.resolve_approval(requested.payload["approval_id"], "allow")

    assert await task == "yes"
    assert worker.emitted[-1].payload["decision"] == "allow"


async def test_denied_approval_returns_no(tmp_path):
    worker = _worker(tmp_path)
    task = asyncio.create_task(worker._ask_approval(_request()))
    await asyncio.sleep(0)

    worker.resolve_approval(worker.emitted[0].payload["approval_id"], "deny")

    assert await task == "no"
    assert worker.emitted[-1].payload["decision"] == "deny"


async def test_approve_all_still_emits_a_complete_audit_trail(tmp_path):
    """Auto-approvals round-trip so the transcript records what was granted."""
    worker = _worker(tmp_path)
    worker.approve_all = True

    assert await worker._ask_approval(_request()) == "yes"

    types = [e.type for e in worker.emitted]
    assert types == ["approval.requested", "approval.resolved"]
    assert worker.emitted[0].payload["auto"] is True
    assert worker.emitted[1].payload["decision"] == "auto"


async def test_always_allow_short_circuits_only_that_tool(tmp_path):
    worker = _worker(tmp_path)
    worker.always_allow.add("write_file")

    assert await worker._ask_approval(_request("write_file")) == "yes"

    pending = asyncio.create_task(worker._ask_approval(_request("run_command", "c2")))
    await asyncio.sleep(0)
    assert not pending.done()
    worker.resolve_approval(worker.emitted[-1].payload["approval_id"], "deny")
    assert await pending == "no"


async def test_approval_times_out_into_a_denial(tmp_path, monkeypatch):
    """A closed window must not leave the worker holding a sequential lock."""
    monkeypatch.setattr("vikram.session_worker.APPROVAL_TIMEOUT_SECONDS", 0.05)
    worker = _worker(tmp_path)

    assert await worker._ask_approval(_request()) == "no"
    assert worker.emitted[-1].payload["decision"] == "timeout"


async def test_resolving_an_unknown_approval_is_ignored(tmp_path):
    worker = _worker(tmp_path)

    worker.resolve_approval("nope", "allow")  # must not raise


async def test_sequence_numbers_are_monotonic(tmp_path):
    worker = _worker(tmp_path)
    worker.approve_all = True

    await worker._ask_approval(_request())
    await worker._ask_approval(_request(call_id="c2"))

    seqs = [e.seq for e in worker.emitted]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


# --- worker: commands --------------------------------------------------


async def test_set_flags_toggles_approve_all_live(tmp_path):
    worker = _worker(tmp_path)

    await worker.handle({"cmd": "set_flags", "approve_all": True})
    assert worker.approve_all is True

    await worker.handle({"cmd": "set_flags", "approve_all": False})
    assert worker.approve_all is False


async def test_allow_always_records_the_tool(tmp_path):
    worker = _worker(tmp_path)
    task = asyncio.create_task(worker._ask_approval(_request()))
    await asyncio.sleep(0)

    await worker.handle(
        {
            "cmd": "approve",
            "approval_id": worker.emitted[0].payload["approval_id"],
            "decision": "allow_always",
            "tool_name": "write_file",
        }
    )

    assert await task == "yes"
    assert "write_file" in worker.always_allow


async def test_shutdown_stops_the_loop(tmp_path):
    worker = _worker(tmp_path)

    assert await worker.handle({"cmd": "shutdown"}) is False
    assert await worker.handle({"cmd": "cancel"}) is True


async def test_unknown_command_is_ignored(tmp_path):
    worker = _worker(tmp_path)

    assert await worker.handle({"cmd": "nonsense"}) is True


# --- turns -------------------------------------------------------------


class _FakeAgent:
    name = "Coder"
    model_config = {"provider": "ollama", "model": "m"}
    tool_names = ["read_file"]
    approval_tool_names = []
    mcp_clients = []

    def __init__(self, events):
        self._events = events

    async def stream_events(self, prompt, **kwargs):
        for event in self._events:
            await asyncio.sleep(0)
            yield event


class _FakeResult:
    output = "done"

    def all_messages(self):
        return []

    def usage(self):
        from types import SimpleNamespace

        return SimpleNamespace(input_tokens=11, output_tokens=7, total_tokens=18)


async def test_turn_streams_deltas_then_finishes_with_usage(tmp_path):
    worker = _worker(tmp_path)
    worker.agent = _FakeAgent(
        [
            {"data": "he"},
            {"data": "llo"},
            {"vikram_result": _FakeResult()},
        ]
    )

    await worker.run_turn("t1", "hi")

    types = [e.type for e in worker.emitted]
    assert types == ["turn.started", "text.delta", "text.delta", "turn.finished"]
    finished = worker.emitted[-1].payload
    assert finished["output"] == "done"
    assert finished["usage"] == {
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
    }
    assert finished["ttft_ms"] is not None
    assert finished["ttft_ms"] <= finished["duration_ms"]


async def test_turn_reports_a_failure_rather_than_dying(tmp_path):
    class _Boom:
        async def stream_events(self, prompt, **kwargs):
            raise RuntimeError("model exploded")
            yield  # pragma: no cover

    worker = _worker(tmp_path)
    worker.agent = _Boom()

    await worker.run_turn("t1", "hi")

    assert worker.emitted[-1].type == "turn.failed"
    assert worker.emitted[-1].payload["error_type"] == "RuntimeError"


async def test_cancelling_a_turn_emits_cancelled(tmp_path):
    class _Slow:
        async def stream_events(self, prompt, **kwargs):
            yield {"data": "x"}
            await asyncio.sleep(30)

    worker = _worker(tmp_path)
    worker.agent = _Slow()
    await worker.handle({"cmd": "prompt", "turn_id": "t1", "prompt": "hi"})
    await asyncio.sleep(0.05)

    worker.cancel_turn()
    with pytest.raises(asyncio.CancelledError):
        await worker._turn

    assert worker.emitted[-1].type == "turn.cancelled"


async def test_usage_survives_a_provider_without_token_counts(tmp_path):
    class _NoUsage:
        output = "done"

        def all_messages(self):
            return []

        def usage(self):
            raise AttributeError("not supported")

    worker = _worker(tmp_path)
    worker.agent = _FakeAgent([{"vikram_result": _NoUsage()}])

    await worker.run_turn("t1", "hi")

    assert worker.emitted[-1].payload["usage"] == {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
    }


# --- SSE ---------------------------------------------------------------


def _session_double():
    session = Session.__new__(Session)
    session.id = "s1"
    session.closed = False
    session._listeners = set()
    session._backlog = []
    return session


async def test_sse_frames_carry_event_name_and_id():
    session = _session_double()
    stream = sse_stream(session)
    queue = None

    async def pump():
        nonlocal queue
        frame = await stream.__anext__()
        return frame

    task = asyncio.create_task(pump())
    await asyncio.sleep(0.01)
    session._broadcast({"type": "text.delta", "seq": 4, "payload": {"text": "hi"}})
    frame = await task

    assert "event: text.delta" in frame
    assert "id: 4" in frame
    assert json.loads(frame.split("data: ", 1)[1].strip())["payload"] == {"text": "hi"}
    await stream.aclose()


async def test_a_late_listener_still_learns_what_the_session_is():
    """Without a backlog the UI would have no model or tool list on reconnect."""
    session = _session_double()
    session._broadcast(
        {"type": "session.ready", "seq": 1, "payload": {"name": "Coder"}}
    )

    queue = session.attach()

    assert queue.get_nowait()["payload"]["name"] == "Coder"


async def test_a_slow_listener_is_dropped_not_backed_up():
    session = _session_double()
    queue = session.attach()
    for _ in range(queue.maxsize):
        queue.put_nowait({"type": "text.delta"})

    session._broadcast({"type": "text.delta", "seq": 1, "payload": {}})

    assert queue not in session._listeners


async def test_sending_to_a_closed_session_raises():
    session = _session_double()
    session.closed = True
    session.process = None

    with pytest.raises(SessionError, match="closed"):
        await session.send({"cmd": "cancel"})


async def test_usage_reads_the_property_shape(tmp_path):
    """Regression: reading usage as a method reported None for every count."""
    from pydantic_ai.usage import RunUsage

    class _PropertyUsageResult:
        output = "done"
        usage = RunUsage(input_tokens=6001, output_tokens=104)

        def all_messages(self):
            return []

    worker = _worker(tmp_path)
    worker.agent = _FakeAgent([{"vikram_result": _PropertyUsageResult()}])

    await worker.run_turn("t1", "hi")

    assert worker.emitted[-1].payload["usage"] == {
        "input_tokens": 6001,
        "output_tokens": 104,
        "total_tokens": 6105,
    }
