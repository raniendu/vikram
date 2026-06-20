import importlib
import json
import stat
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
    assert "vikram configure" in result.stdout


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

    def run_sync(self, prompt, **kwargs):
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


def test_cli_configure_writes_ollama_local_config(monkeypatch, tmp_path, capsys):
    from vikram.cli import main

    answers = iter(["ollama", "llama3.2", "http://localhost:11434"])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    main(["configure"])

    config_path = tmp_path / "vikram" / "config.toml"
    assert config_path.is_file()
    assert config_path.read_text(encoding="utf-8") == (
        "# Written by `vikram configure`.\n"
        'model_provider = "ollama"\n'
        'model = "llama3.2"\n'
        'ollama_base_url = "http://localhost:11434"\n'
    )
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert str(config_path) in capsys.readouterr().out


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


def _patch_interactive_io(monkeypatch, tmp_path):
    """Stub the prompt/rich plumbing so run_interactive exits after one turn."""
    import prompt_toolkit
    import prompt_toolkit.history
    import pydantic_ai._cli
    import rich.console

    from vikram import cli

    class _EOFSession:
        def __init__(self, *args, **kwargs):
            pass

        async def prompt_async(self, *args, **kwargs):
            raise EOFError

    class _SilentConsole:
        def print(self, *args, **kwargs):
            pass

    monkeypatch.setattr(prompt_toolkit, "PromptSession", _EOFSession)
    monkeypatch.setattr(prompt_toolkit.history, "FileHistory", lambda *a, **k: None)
    monkeypatch.setattr(pydantic_ai._cli, "CustomAutoSuggest", lambda *a, **k: None)
    monkeypatch.setattr(
        pydantic_ai._cli, "handle_slash_command", lambda *a, **k: (None, False)
    )
    monkeypatch.setattr(rich.console, "Console", _SilentConsole)
    monkeypatch.setattr(cli, "HISTORY_PATH", tmp_path / "hist")


class _RecordingAgent:
    def __init__(self):
        self.enter_count = 0

    async def __aenter__(self):
        self.enter_count += 1
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.parametrize("keep_warm", [True, False])
async def test_run_interactive_keeps_servers_warm(monkeypatch, tmp_path, keep_warm):
    from vikram.cli import run_interactive

    _patch_interactive_io(monkeypatch, tmp_path)
    agent = _RecordingAgent()

    await run_interactive(
        agent, prog_name="Demo", quiet=False, keep_servers_warm=keep_warm
    )

    # The agent context is entered exactly once for the session only when MCP
    # servers need to stay connected across turns.
    assert agent.enter_count == (1 if keep_warm else 0)


@pytest.mark.asyncio
async def test_cli_render_turn_returns_context_percentage():
    import contextlib

    from vikram.cli import _render_turn
    from vikram.settings import VikramSettings

    class FakeUsage:
        input_tokens = 450

    class FakeResult:
        output = "hello output"

        def all_messages(self):
            return []

        def usage(self):
            return FakeUsage()

    class FakeAgent:
        @contextlib.asynccontextmanager
        async def iter(self, prompt, message_history, capabilities):
            class FakeRun:
                ctx = None
                result = FakeResult()

                async def __aiter__(self):
                    if False:
                        yield None

            yield FakeRun()

    class FakeConsole:
        def print(self, *args, **kwargs):
            pass

    console = FakeConsole()
    settings = VikramSettings(
        _env_file=None,
        VIKRAM_CONTEXT_WINDOW_TOKENS=1000,
        VIKRAM_CONTEXT_WARNING_RATIO=0.1,
    )

    _, percent = await _render_turn(
        FakeAgent(),
        "test prompt",
        [],
        console,
        quiet=False,
        settings=settings,
    )

    assert percent == 45


@pytest.mark.asyncio
async def test_run_interactive_prompts_with_context_usage(monkeypatch, tmp_path):
    import contextlib

    import prompt_toolkit
    import prompt_toolkit.history
    import pydantic_ai._cli
    import rich.console

    from vikram.cli import run_interactive
    from vikram.settings import VikramSettings

    prompts_requested = []

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def prompt_async(self, prompt, **kwargs):
            prompts_requested.append(prompt)
            if len(prompts_requested) == 1:
                return "hello"
            raise EOFError

    class SilentConsole:
        def print(self, *args, **kwargs):
            pass

    monkeypatch.setattr(prompt_toolkit, "PromptSession", FakeSession)
    monkeypatch.setattr(prompt_toolkit.history, "FileHistory", lambda *a, **k: None)
    monkeypatch.setattr(pydantic_ai._cli, "CustomAutoSuggest", lambda *a, **k: None)
    monkeypatch.setattr(
        pydantic_ai._cli, "handle_slash_command", lambda *a, **k: (None, False)
    )
    from vikram import cli

    monkeypatch.setattr(rich.console, "Console", SilentConsole)
    monkeypatch.setattr(cli, "HISTORY_PATH", tmp_path / "hist")

    class FakeUsage:
        input_tokens = 200

    class FakeResult:
        output = "reply"

        def all_messages(self):
            return []

        def usage(self):
            return FakeUsage()

    class FakeAgent:
        @contextlib.asynccontextmanager
        async def iter(self, prompt, message_history, capabilities):
            class FakeRun:
                ctx = None
                result = FakeResult()

                async def __aiter__(self):
                    if False:
                        yield None

            yield FakeRun()

    settings = VikramSettings(
        _env_file=None,
        VIKRAM_CONTEXT_WINDOW_TOKENS=1000,
        VIKRAM_CONTEXT_WARNING_RATIO=0.1,
    )

    await run_interactive(
        FakeAgent(),
        prog_name="DemoAgent",
        quiet=False,
        keep_servers_warm=False,
        settings=settings,
    )

    assert len(prompts_requested) == 2
    assert prompts_requested[0] == "DemoAgent (0%) ➤ "
    assert prompts_requested[1] == "DemoAgent (20%) ➤ "
