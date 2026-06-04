import importlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import DeferredToolRequests, ToolApproved, ToolDenied

APP_ROOT = Path(__file__).resolve().parents[1]


def test_cli_file_execution_shows_help():
    result = subprocess.run(
        [sys.executable, str(APP_ROOT / "vikram" / "cli.py"), "--help"],
        capture_output=True,
        check=False,
        cwd=APP_ROOT,
        text=True,
    )

    assert result.returncode == 0
    assert "usage: vikram" in result.stdout
    assert "--agent" in result.stdout
    assert "--once" in result.stdout
    assert "--prompt" in result.stdout
    assert "--json" in result.stdout


class FakeSettings:
    default_agent = "vikram"
    spec_root = APP_ROOT / "spec"

    def model_copy(self, *, update):
        copied = FakeSettings()
        copied.default_agent = update.get("default_agent", self.default_agent)
        copied.spec_root = self.spec_root
        return copied


class FakeAgent:
    def __init__(self, calls):
        self.calls = calls

    def run_sync(self, prompt):
        self.calls.append(prompt)
        return SimpleNamespace(output=f"reply: {prompt}")

    def to_cli_sync(self, *, prog_name):
        self.calls.append(f"interactive:{prog_name}")


def patch_cli_dependencies(monkeypatch):
    calls = []

    agent_module = importlib.import_module("vikram.agent")
    settings_module = importlib.import_module("vikram.settings")
    spec_module = importlib.import_module("vikram.spec")

    monkeypatch.setattr(settings_module, "VikramSettings", FakeSettings)
    monkeypatch.setattr(
        spec_module,
        "load_spec",
        lambda name, spec_root: SimpleNamespace(name=name.title()),
    )
    monkeypatch.setattr(
        agent_module,
        "build_agent",
        lambda *, spec, settings: FakeAgent(calls),
    )
    return calls


def test_cli_once_runs_prompt_string(monkeypatch, capsys):
    from vikram.cli import main

    calls = patch_cli_dependencies(monkeypatch)

    main(["--once", "--prompt", "say hello"])

    assert calls == ["say hello"]
    assert capsys.readouterr().out == "reply: say hello\n"


def test_cli_once_reads_prompt_from_file(monkeypatch, capsys, tmp_path):
    from vikram.cli import main

    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("from file\n", encoding="utf-8")
    calls = patch_cli_dependencies(monkeypatch)

    main(["--once", "--prompt", str(prompt_file)])

    assert calls == ["from file\n"]
    assert capsys.readouterr().out == "reply: from file\n\n"


def test_cli_once_reads_prompt_from_at_file(monkeypatch, capsys, tmp_path):
    from vikram.cli import main

    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("from at-file", encoding="utf-8")
    calls = patch_cli_dependencies(monkeypatch)

    main(["--once", "--prompt", f"@{prompt_file}"])

    assert calls == ["from at-file"]
    assert capsys.readouterr().out == "reply: from at-file\n"


def test_cli_once_json_outputs_agent_and_output(monkeypatch, capsys):
    from vikram.cli import main

    calls = patch_cli_dependencies(monkeypatch)

    main(["--agent", "coder", "--once", "--prompt", "status", "--json"])

    assert calls == ["status"]
    assert json.loads(capsys.readouterr().out) == {
        "agent": "Coder",
        "output": "reply: status",
    }


def test_prompt_requires_once(capsys):
    from vikram.cli import main

    try:
        main(["--prompt", "status"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("Expected parser error")

    assert "--prompt requires --once" in capsys.readouterr().err


def test_quiet_rejects_once(capsys):
    from vikram.cli import main

    try:
        main(["--once", "--prompt", "hi", "--quiet"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("Expected parser error")

    assert "--quiet cannot be combined with --once" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_cli_deferred_tool_handler_prompts_for_approval():
    from vikram.cli import _resolve_deferred_tool_requests

    class FakeSession:
        def __init__(self):
            self.answers = ["y", "no"]
            self.prompts = []

        async def prompt_async(self, prompt, **kwargs):
            self.prompts.append((prompt, kwargs))
            return self.answers.pop(0)

    class FakeConsole:
        def __init__(self):
            self.messages = []

        def print(self, *args, **kwargs):
            self.messages.append((args, kwargs))

    session = FakeSession()
    console = FakeConsole()
    requests = DeferredToolRequests(
        approvals=[
            ToolCallPart(
                "write_file",
                {"path": "notes.txt", "content": "hello"},
                tool_call_id="call-1",
            ),
            ToolCallPart(
                "run_command",
                {"command": "git status --short"},
                tool_call_id="call-2",
            ),
        ]
    )

    results = await _resolve_deferred_tool_requests(
        None, requests, session=session, console=console
    )

    assert isinstance(results.approvals["call-1"], ToolApproved)
    assert isinstance(results.approvals["call-2"], ToolDenied)
    assert len(session.prompts) == 2
