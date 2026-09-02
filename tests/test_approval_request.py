from __future__ import annotations

import json

from vikram.agent import ApprovalRequest


def test_str_reproduces_the_legacy_prompt_verbatim():
    """approval_ask callers must keep seeing the exact string they always did."""
    request = ApprovalRequest(
        tool_name="write_file",
        tool_call_id="call-1",
        args={"path": "notes.md", "content": "hi"},
    )

    expected = 'Tool "write_file" requires human approval. Input: ' + json.dumps(
        {"path": "notes.md", "content": "hi"}, default=str
    )
    assert str(request) == expected


def test_carries_structured_fields_a_gui_dialog_needs():
    request = ApprovalRequest(
        tool_name="run_command",
        tool_call_id="call-2",
        args={"command": "rm -rf build"},
    )

    assert request.tool_name == "run_command"
    assert request.tool_call_id == "call-2"
    assert request.args["command"] == "rm -rf build"


def test_str_survives_non_serialisable_args():
    request = ApprovalRequest(
        tool_name="edit_file",
        tool_call_id="call-3",
        args={"path": object()},
    )

    assert str(request).startswith('Tool "edit_file" requires human approval.')
