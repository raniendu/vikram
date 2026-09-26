"""File tools served by the harness FileSystem capability (vikram/file_tools.py).

These keep the guarantees the old hand-written tools had: stable tool names,
workspace confinement, secret/excluded paths refused (as text, never a run
failure), writes behind Vikram's approval prompt, and logs by path and size.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.capabilities import HandleDeferredToolCalls
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from tests.conftest import find_log_event
from vikram.agent import build_agent
from vikram.file_tools import build_file_capability, is_denied_path
from vikram.hooks import HookSpec, build_hooks
from vikram.settings import VikramSettings
from vikram.spec import load_spec

ALL = ["read_file", "glob", "grep", "write_file", "edit_file"]
SPEC_ROOT = Path(__file__).resolve().parent.parent / "spec"


@pytest.fixture
def workspace(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "ws"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "example.py").write_text("alpha\nbeta\ngamma\n")
    (root / ".env.local").write_text("TOKEN=s3cret\n")
    (root / ".env.example").write_text("TOKEN=\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    (tmp_path / "outside.txt").write_text("outside\n")
    monkeypatch.chdir(root)
    return root


def _run(calls: list[tuple[str, dict]], *, approve: bool = True, tools=ALL, **kw):
    """Run one scripted turn; return {call index: tool result text}."""
    asked: list[str] = []

    def model(messages, info):
        returned = [
            p
            for m in messages
            for p in getattr(m, "parts", [])
            if isinstance(p, ToolReturnPart)
        ]
        if not returned:
            return ModelResponse(
                parts=[
                    ToolCallPart(name, args, tool_call_id=f"c{i}")
                    for i, (name, args) in enumerate(calls)
                ]
            )
        return ModelResponse(parts=[TextPart("done")])

    async def handler(ctx, requests):
        asked.extend(call.tool_name for call in requests.approvals)
        return requests.build_results(
            approvals={c.tool_call_id: approve for c in requests.approvals}, calls={}
        )

    agent = Agent(
        FunctionModel(model),
        capabilities=[
            HandleDeferredToolCalls(handler=handler),
            build_file_capability(tools, **kw),
        ],
    )
    result = agent.run_sync("go")
    returns = {
        p.tool_call_id: str(p.content)
        for m in result.all_messages()
        for p in getattr(m, "parts", [])
        if isinstance(p, ToolReturnPart)
    }
    return {int(k[1:]): v for k, v in returns.items()}, asked


def test_tool_names_stay_stable_and_writes_are_sequential(workspace):
    offered: list[Any] = []

    def model(messages, info):
        offered.extend(info.function_tools)
        return ModelResponse(parts=[TextPart("hi")])

    Agent(FunctionModel(model), capabilities=[build_file_capability(ALL)]).run_sync(
        "hi"
    )
    by_name = {tool.name: tool for tool in offered}
    assert set(by_name) == set(ALL)
    assert by_name["write_file"].sequential and by_name["edit_file"].sequential
    assert not by_name["read_file"].sequential


def test_read_returns_numbered_lines(workspace):
    out, _ = _run([("read_file", {"path": "pkg/example.py", "offset": 1, "limit": 2})])
    assert "beta" in out[0] and "gamma" in out[0] and "alpha" not in out[0]


def test_secrets_and_escapes_are_refused_as_text(workspace, log_events):
    out, _ = _run(
        [
            ("read_file", {"path": ".env.local"}),
            ("read_file", {"path": "../outside.txt"}),
            ("read_file", {"path": str(workspace.parent / "outside.txt")}),
            ("read_file", {"path": ".git/config"}),
            ("grep", {"pattern": "x", "path": "../"}),
        ]
    )
    assert out[0].startswith("Refusing:") and "s3cret" not in out[0]
    assert "escapes the workspace" in out[1] and "outside" not in out[1].split("'")[-1]
    assert "escapes the workspace" in out[2]
    assert out[3].startswith("Refusing:")
    assert "escapes the workspace" in out[4]

    refusals = {
        (e["tool"], e["reason"])
        for e in log_events
        if e["event"] == "tool_call_refused"
    }
    assert refusals == {
        ("read_file", "sensitive_path"),
        ("read_file", "path_escapes_workspace"),
        ("grep", "path_escapes_workspace"),
    }


def test_env_example_stays_readable(workspace):
    out, _ = _run([("read_file", {"path": ".env.example"})])
    assert "TOKEN=" in out[0]


def test_symlinks_cannot_escape(workspace):
    (workspace / "link.txt").symlink_to(workspace.parent / "outside.txt")
    out, _ = _run([("read_file", {"path": "link.txt"})])
    assert "escapes the workspace" in out[0]


def test_glob_and_grep_hide_secret_and_excluded_files(workspace):
    out, _ = _run(
        [("glob", {"pattern": "**/*"}), ("grep", {"pattern": "TOKEN|beta|core"})]
    )
    assert "pkg/example.py" in out[0]
    assert ".env" not in out[0] and ".git" not in out[0]
    assert "pkg/example.py:2:beta" in out[1]
    assert "s3cret" not in out[1] and "[core]" not in out[1]


def test_writes_need_approval_and_create_folders(workspace, log_events):
    out, asked = _run(
        [
            ("write_file", {"path": "notes/todo.txt", "content": "hello\n"}),
            (
                "edit_file",
                {"path": "pkg/example.py", "old_text": "beta", "new_text": "B"},
            ),
        ]
    )
    assert asked == ["write_file", "edit_file"]
    assert (workspace / "notes" / "todo.txt").read_text() == "hello\n"
    assert (workspace / "pkg" / "example.py").read_text() == "alpha\nB\ngamma\n"

    written = find_log_event(log_events, "tool_write_file_succeeded")
    assert written["path"] == "notes/todo.txt"
    assert written["content_length"] == 6
    assert written["overwrote_existing"] is False
    edited = find_log_event(log_events, "tool_edit_file_succeeded")
    assert edited["path"] == "pkg/example.py" and edited["replacements"] == 1


def test_denied_approval_leaves_files_alone(workspace):
    out, asked = _run(
        [("write_file", {"path": "pkg/example.py", "content": "gone\n"})],
        approve=False,
    )
    assert asked == ["write_file"]
    assert (workspace / "pkg" / "example.py").read_text() == "alpha\nbeta\ngamma\n"


def test_writes_to_secrets_or_outside_are_refused_even_when_approved(workspace):
    out, _ = _run(
        [
            ("write_file", {"path": ".env", "content": "X=1\n"}),
            ("write_file", {"path": "../evil/x.txt", "content": "x"}),
        ]
    )
    assert out[0].startswith("Refusing:")
    assert "escapes the workspace" in out[1]
    assert not (workspace / ".env").exists()
    assert not (workspace.parent / "evil").exists()


def test_edit_requires_a_unique_match(workspace):
    (workspace / "dup.txt").write_text("same\nsame\n")
    out, _ = _run(
        [("edit_file", {"path": "dup.txt", "old_text": "same", "new_text": "x"})]
    )
    assert (workspace / "dup.txt").read_text() == "same\nsame\n"
    assert not out[0].startswith("Refusing:")


def test_tool_hooks_still_wrap_file_tools(workspace, tmp_path):
    hook_module = tmp_path / "filehooks.py"
    hook_module.write_text(
        "SEEN = []\n"
        "def pre(payload):\n"
        "    SEEN.append(payload['tool_name'])\n"
        "    if payload['tool_name'] == 'write_file':\n"
        "        return {'decision': 'block', 'reason': 'no writes today'}\n"
    )
    import sys

    sys.path.insert(0, str(tmp_path))
    try:
        hooks = build_hooks(
            [
                HookSpec(
                    event="PreToolUse", transport="python", entrypoint="filehooks:pre"
                )
            ]
        )
        out, _ = _run(
            [
                ("read_file", {"path": "pkg/example.py"}),
                ("write_file", {"path": "a.txt", "content": "x"}),
            ],
            hooks=hooks,
        )
        import filehooks

        assert "read_file" in filehooks.SEEN and "write_file" in filehooks.SEEN
        assert not (workspace / "a.txt").exists()
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("filehooks", None)


@pytest.mark.parametrize(
    ("path", "denied"),
    [
        (".env", True),
        ("app/.env.production", True),
        (".env.example", False),
        ("keys/server.pem", True),
        (".ssh/config", True),
        ("secrets/token.txt", True),
        ("node_modules/x/index.js", True),
        ("src/main.py", False),
    ],
)
def test_denied_path_rules(path, denied):
    assert is_denied_path(path) is denied


def test_coder_builds_with_file_capability(workspace, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    for name in ("VIKRAM_MODEL", "VIKRAM_MODEL_PROVIDER", "VIKRAM_SPEC_ROOT"):
        monkeypatch.delenv(name, raising=False)
    agent = build_agent(
        spec=load_spec("coder", SPEC_ROOT),
        settings=VikramSettings(_env_file=None),
        surface="cli",
    )
    assert {"read_file", "glob", "grep", "write_file", "edit_file"} <= set(
        agent.tool_names
    )
    assert {"write_file", "edit_file"} <= set(agent.approval_tool_names)


def test_bundled_ripgrep_is_appended_to_path_only_when_missing(monkeypatch, tmp_path):
    import os
    import sys

    from vikram import file_tools

    fake_bin = tmp_path / "venv-bin"
    fake_bin.mkdir()
    (fake_bin / "rg").write_text("#!/bin/sh\n")
    (fake_bin / "rg").chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(fake_bin / "python"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    assert file_tools.ensure_ripgrep_on_path() is True
    parts = os.environ["PATH"].split(os.pathsep)
    assert parts == [str(tmp_path / "empty"), str(fake_bin)]  # appended, not first

    assert file_tools.ensure_ripgrep_on_path() is True  # already findable
    assert os.environ["PATH"].split(os.pathsep) == parts
