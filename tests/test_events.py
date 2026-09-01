from __future__ import annotations

from vikram.events import Event, map_stream_event


def _map(raw, **kwargs):
    return map_stream_event(raw, seq=1, session_id="s1", **kwargs)


def test_maps_text_delta():
    event = _map({"data": "hello"})

    assert isinstance(event, Event)
    assert event.type == "text.delta"
    assert event.payload == {"text": "hello"}
    assert event.session_id == "s1"
    assert event.seq == 1


def test_maps_thinking_delta():
    event = _map({"reasoningText": "considering"})

    assert event.type == "thinking.delta"
    assert event.payload == {"text": "considering"}


def test_maps_tool_call():
    event = _map(
        {
            "current_tool_use": {
                "toolUseId": "call-1",
                "name": "write_file",
                "input": {"path": "a.txt"},
            }
        }
    )

    assert event.type == "tool.call"
    assert event.payload == {
        "tool_use_id": "call-1",
        "name": "write_file",
        "input": {"path": "a.txt"},
    }


def test_maps_tool_result_and_joins_content_blocks():
    event = _map(
        {
            "tool_result": {
                "toolUseId": "call-1",
                "status": "error",
                "content": [{"text": "boom"}, {"text": " happened"}],
            }
        }
    )

    assert event.type == "tool.result"
    assert event.payload["status"] == "error"
    assert event.payload["text"] == "boom happened"


def test_maps_bedrock_shaped_tool_use():
    """streaming.tool_use_from_event tolerates the raw Bedrock envelope."""
    event = _map(
        {
            "event": {
                "contentBlockStart": {
                    "start": {"toolUse": {"toolUseId": "c2", "name": "grep"}}
                }
            }
        }
    )

    assert event.type == "tool.call"
    assert event.payload["tool_use_id"] == "c2"


def test_terminal_sentinel_is_not_mapped():
    """The caller owns vikram_result: only it can read usage off the result."""
    assert _map({"vikram_result": object()}) is None


def test_unknown_event_is_dropped():
    assert _map({"something_else": 1}) is None


def test_column_id_is_carried_for_playground_fanout():
    event = _map({"data": "hi"}, column_id="col-2", turn_id="t1")

    assert event.column_id == "col-2"
    assert event.turn_id == "t1"


def test_event_is_json_serialisable():
    event = _map({"data": "hi"})

    assert event.model_dump_json()
