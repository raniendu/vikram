import importlib
import io
import json
import os
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    assert "doctor" in result.stdout
    assert "Check configuration and workspace health" in result.stdout


@pytest.mark.asyncio
async def test_command_auto_suggest_supports_prompt_toolkit_async_protocol():
    from prompt_toolkit.document import Document

    from vikram.cli import _CommandAutoSuggest

    suggestion = await _CommandAutoSuggest(["/help"]).get_suggestion_async(
        None, Document("/he")
    )

    assert suggestion is not None
    assert suggestion.text == "lp"


class FakeSettings:
    default_agent = "vikram"
    spec_root = APP_ROOT / "spec"
    model = None

    def model_copy(self, *, update):
        copied = FakeSettings()
        copied.default_agent = update.get("default_agent", self.default_agent)
        copied.spec_root = self.spec_root
        copied.model = update.get("model", self.model)
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
        lambda *, spec, settings, **kwargs: FakeAgent(calls),
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


def test_cli_exec_runs_positional_prompt(monkeypatch, capsys):
    from vikram.cli import main

    calls = patch_cli_dependencies(monkeypatch)

    main(["exec", "say hello"])

    assert calls == ["say hello"]
    assert capsys.readouterr().out == "reply: say hello\n"


def test_cli_exec_reads_omitted_prompt_from_stdin(monkeypatch, capsys):
    from vikram.cli import main

    calls = patch_cli_dependencies(monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO("from stdin"))

    main(["exec"])

    assert calls == ["from stdin"]
    assert capsys.readouterr().out == "reply: from stdin\n"


def test_cli_exec_adds_piped_stdin_to_positional_prompt(monkeypatch, capsys):
    from vikram.cli import main

    calls = patch_cli_dependencies(monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO("patch contents"))

    main(["exec", "review this patch"])

    assert calls == [
        "review this patch\n\nAdditional context from stdin:\npatch contents"
    ]


def test_cli_exec_writes_last_message_and_json(monkeypatch, capsys, tmp_path):
    from vikram.cli import main

    calls = patch_cli_dependencies(monkeypatch)
    output_path = tmp_path / "last-message.md"

    main(
        [
            "exec",
            "status",
            "--json",
            "--output-last-message",
            str(output_path),
        ]
    )

    assert calls == ["status"]
    assert output_path.read_text(encoding="utf-8") == "reply: status"
    assert json.loads(capsys.readouterr().out) == {
        "agent": "Vikram",
        "output": "reply: status",
    }


def test_cli_exec_applies_model_and_directory_overrides(monkeypatch, tmp_path):
    from vikram.cli import main

    observed = {}
    original_cwd = Path.cwd()
    agent_module = importlib.import_module("vikram.agent")
    settings_module = importlib.import_module("vikram.settings")
    spec_module = importlib.import_module("vikram.spec")

    monkeypatch.setattr(settings_module, "VikramSettings", FakeSettings)
    monkeypatch.setattr(
        spec_module,
        "load_spec",
        lambda name, spec_root: SimpleNamespace(name=name.title()),
    )

    def build_agent(*, spec, settings, **kwargs):
        observed.update(model=settings.model, cwd=Path.cwd())
        return FakeAgent([])

    monkeypatch.setattr(agent_module, "build_agent", build_agent)

    main(["exec", "-C", str(tmp_path), "-m", "test-model", "status"])

    assert observed == {"model": "test-model", "cwd": tmp_path.resolve()}
    assert Path.cwd() == original_cwd


def test_cli_configure_writes_ollama_local_config(monkeypatch, tmp_path, capsys):
    from vikram.cli import main

    answers = iter(["1", "llama3.2", "http://localhost:11434", ""])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    main(["configure"])

    config_path = tmp_path / "vikram" / "config.toml"
    assert config_path.is_file()
    text = config_path.read_text(encoding="utf-8")
    assert text.startswith("# Written by `vikram configure`.")
    assert tomllib.loads(text) == {
        "config_version": 2,
        "default_provider": "ollama",
        "providers": {
            "ollama": {
                "model": "llama3.2",
                "base_url": "http://localhost:11434",
            }
        },
    }
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert str(config_path) in capsys.readouterr().out


def test_cli_setup_adds_provider_and_preserves_existing(monkeypatch, tmp_path):
    from vikram.cli import main

    config_dir = tmp_path / "vikram"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        "\n".join(
            [
                "config_version = 2",
                'default_provider = "ollama"',
                "",
                "[providers.ollama]",
                'model = "llama3.2"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    answers = iter(["anthropic", "claude-sonnet-5", "", "anthropic"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "sk-ant-test")

    main(["setup"])

    data = tomllib.loads((config_dir / "config.toml").read_text(encoding="utf-8"))
    assert data["default_provider"] == "anthropic"
    assert data["providers"]["ollama"] == {"model": "llama3.2"}
    assert data["providers"]["anthropic"] == {
        "model": "claude-sonnet-5",
        "api_key": "sk-ant-test",
    }


def test_cli_configure_without_changes_writes_nothing(monkeypatch, tmp_path, capsys):
    from vikram.cli import main

    answers = iter([""])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    main(["configure"])

    assert not (tmp_path / "vikram" / "config.toml").exists()
    assert "No changes made" in capsys.readouterr().out


def test_prompt_requires_once(capsys):
    from vikram.cli import main

    try:
        main(["--prompt", "status"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("Expected parser error")

    assert "--prompt requires --once" in capsys.readouterr().err


def test_cli_suggests_close_command_name(capsys):
    from vikram.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main(["docter"])

    assert exc_info.value.code == 2
    error = capsys.readouterr().err
    assert "unknown command: docter" in error
    assert "Did you mean `vikram doctor`?" in error


def test_cli_exec_empty_prompt_has_recovery_example(monkeypatch, capsys):
    from vikram.cli import main

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    with pytest.raises(SystemExit) as exc_info:
        main(["exec"])

    assert exc_info.value.code == 2
    error = capsys.readouterr().err
    assert "pass a task as an argument or pipe content on stdin" in error
    assert 'vikram exec "summarize this repo"' in error


def test_quiet_rejects_once(capsys):
    from vikram.cli import main

    try:
        main(["--once", "--prompt", "hi", "--quiet"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("Expected parser error")

    assert "--quiet cannot be combined with --once" in capsys.readouterr().err


def test_tool_result_extracts_compatibility_message_event():
    from vikram.cli import _tool_result_from_event

    result = _tool_result_from_event(
        {
            "message": {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "call-1",
                            "status": "success",
                            "content": [{"text": "done"}],
                        }
                    }
                ],
            }
        }
    )

    assert result == {
        "toolUseId": "call-1",
        "status": "success",
        "content": [{"text": "done"}],
    }


def _patch_interactive_io(monkeypatch, tmp_path):
    """Stub the prompt/rich plumbing so run_interactive exits after one turn."""
    import prompt_toolkit
    import prompt_toolkit.history
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
        async def run(self, prompt, *, message_history):
            return FakeResult()

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
async def test_cli_render_turn_newlines_after_streamed_response():
    from vikram.cli import _render_turn

    class FakeResult:
        def all_messages(self):
            return []

        def usage(self):
            return None

    class FakeAgent:
        async def stream_events(self, prompt, *, message_history):
            yield {"data": "final answer"}
            yield {"vikram_result": FakeResult()}

    class BufferConsole:
        def __init__(self):
            self.output = ""

        def print(self, *args, **kwargs):
            text = str(args[0]) if args else ""
            self.output += text + kwargs.get("end", "\n")

    console = BufferConsole()

    await _render_turn(
        FakeAgent(),
        "test prompt",
        [],
        console,
        quiet=False,
    )

    assert console.output == "final answer\n"


@pytest.mark.asyncio
async def test_run_interactive_prompts_with_context_usage(monkeypatch, tmp_path):
    import prompt_toolkit
    import prompt_toolkit.history
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
        async def run(self, prompt, *, message_history):
            return FakeResult()

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


class _CapturingConsole:
    def __init__(self):
        self.messages = []

    def print(self, *args, **kwargs):
        self.messages.append(args[0] if args else "")


def test_format_call_args_truncates_long_values():
    from vikram.cli import _format_call_args

    tool_use = {
        "name": "write_file",
        "input": {"path": "notes.txt", "content": "x" * 500},
    }

    rendered = _format_call_args(tool_use)

    assert rendered.startswith('path="notes.txt", content=')
    assert "…" in rendered
    # The 500-char content must not be dumped in full onto the tool-call line.
    assert len(rendered) < 200


def test_maybe_warn_context_warns_once_then_resets():
    from vikram.cli import _maybe_warn_context
    from vikram.settings import VikramSettings

    settings = VikramSettings(
        _env_file=None,
        VIKRAM_CONTEXT_WINDOW_TOKENS=1000,
        VIKRAM_CONTEXT_WARNING_RATIO=0.8,
    )
    console = _CapturingConsole()

    # First crossing warns.
    warned = _maybe_warn_context(console, 85, False, settings)
    assert warned is True
    assert len(console.messages) == 1

    # Staying above the threshold does not repeat the warning.
    warned = _maybe_warn_context(console, 90, warned, settings)
    assert warned is True
    assert len(console.messages) == 1

    # Dropping below resets the warned state (e.g. after /clear).
    warned = _maybe_warn_context(console, 50, warned, settings)
    assert warned is False
    assert len(console.messages) == 1

    # A later crossing warns again.
    warned = _maybe_warn_context(console, 88, warned, settings)
    assert warned is True
    assert len(console.messages) == 2


def test_maybe_warn_context_noop_without_settings():
    from vikram.cli import _maybe_warn_context

    console = _CapturingConsole()

    assert _maybe_warn_context(console, 99, False, None) is False
    assert console.messages == []


def test_print_help_lists_all_commands():
    from rich.console import Console

    from vikram.cli import _print_help

    buffer = io.StringIO()
    _print_help(Console(file=buffer, width=100, force_terminal=False))
    output = buffer.getvalue()

    for command in (
        "/help",
        "/status",
        "/new",
        "/markdown",
        "/multiline",
        "/exit",
    ):
        assert command in output


def test_print_status_includes_runtime_details(tmp_path):
    from rich.console import Console

    from vikram.cli import _print_status

    settings = SimpleNamespace(
        model="qwen3",
        model_provider="ollama",
        context_window_tokens=32_000,
    )
    buffer = io.StringIO()
    old_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        _print_status(
            Console(file=buffer, width=240, force_terminal=False),
            "Coder",
            settings,
            25,
        )
    finally:
        os.chdir(old_cwd)

    output = buffer.getvalue()
    for expected in ("Coder", "qwen3", "ollama", "25%", str(tmp_path)):
        assert expected in output


def test_last_assistant_text_reads_compatibility_messages():
    from vikram.cli import _last_assistant_text

    messages = [
        {"role": "user", "content": [{"text": "question"}]},
        {
            "role": "assistant",
            "content": [{"text": "first line"}, {"text": "second line"}],
        },
    ]

    assert _last_assistant_text(messages) == "first line\nsecond line"


def test_copy_command_copies_last_assistant_reply(monkeypatch):
    from vikram.cli import _handle_slash_command

    copied = []
    monkeypatch.setattr(
        "vikram.cli._copy_to_clipboard", lambda value: copied.append(value)
    )
    console = _CapturingConsole()

    should_exit, multiline = _handle_slash_command(
        "/copy",
        [{"role": "assistant", "content": [{"text": "answer"}]}],
        False,
        console,
    )

    assert (should_exit, multiline) == (False, False)
    assert copied == ["answer"]
    assert "Copied" in console.messages[0]


def test_diff_command_renders_working_tree(monkeypatch):
    from rich.console import Console

    from vikram.cli import _handle_slash_command

    monkeypatch.setattr("vikram.cli._git_diff", lambda: " M vikram/cli.py\n@@ -1 +1 @@")
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False)

    should_exit, multiline = _handle_slash_command("/diff", [], False, console)

    assert (should_exit, multiline) == (False, False)
    assert "vikram/cli.py" in buffer.getvalue()


def test_print_banner_includes_model_and_provider():
    import io

    from rich.console import Console

    from vikram.cli import _print_banner

    settings = SimpleNamespace(model="llama3.2", model_provider="ollama")
    buffer = io.StringIO()
    _print_banner(
        Console(file=buffer, width=100, force_terminal=False), "Vikram", settings
    )
    output = buffer.getvalue()

    assert "Vikram" in output
    assert "llama3.2" in output
    assert "ollama" in output
    assert "/help" in output


def test_print_banner_resolves_model_from_provider_models():
    import io

    from rich.console import Console

    from vikram.cli import _print_banner

    settings = SimpleNamespace(
        model=None,
        model_provider="anthropic",
        provider_models={"anthropic": "claude-sonnet-5"},
    )
    buffer = io.StringIO()
    _print_banner(
        Console(file=buffer, width=100, force_terminal=False), "Vikram", settings
    )
    output = buffer.getvalue()

    assert "claude-sonnet-5" in output
    assert "anthropic" in output


class _FakeModelSettings:
    """Settings double for /model tests: getattr + model_copy, no env I/O."""

    context_window_tokens = 0

    def __init__(self, model_provider=None, model=None, provider_models=None):
        self.model_provider = model_provider
        self.model = model
        self.provider_models = provider_models or {}

    def model_copy(self, *, update):
        copied = _FakeModelSettings(
            self.model_provider, self.model, dict(self.provider_models)
        )
        for key, value in update.items():
            setattr(copied, key, value)
        return copied


class _SwitchedAgent:
    model_config = {"provider": "stub"}


async def _run_model_command(arg, settings, rebuild, **kwargs):
    from vikram.cli import _handle_model_command

    console = _CapturingConsole()
    old_agent = object()
    agent, new_settings = await _handle_model_command(
        arg,
        old_agent,
        settings,
        console,
        rebuild_agent=rebuild,
        **kwargs,
    )
    return agent, new_settings, console, old_agent


async def test_model_command_lists_current_and_configured_providers():
    settings = _FakeModelSettings(
        model_provider="anthropic",
        provider_models={"anthropic": "claude-sonnet-5", "ollama": "llama3.2"},
    )

    agent, new_settings, console, old_agent = await _run_model_command(
        "", settings, lambda s: _SwitchedAgent()
    )

    assert agent is old_agent
    assert new_settings is settings
    output = "\n".join(str(message) for message in console.messages)
    assert "claude-sonnet-5" in output
    assert "ollama" in output
    assert "not configured" in output


async def test_model_command_switches_to_configured_provider():
    settings = _FakeModelSettings(
        model_provider="anthropic",
        provider_models={"anthropic": "claude-sonnet-5", "ollama": "llama3.2"},
    )
    rebuilds = []

    def rebuild(new_settings):
        rebuilds.append(new_settings)
        return _SwitchedAgent()

    agent, new_settings, console, _ = await _run_model_command(
        "ollama", settings, rebuild
    )

    assert isinstance(agent, _SwitchedAgent)
    assert new_settings.model_provider == "ollama"
    assert new_settings.model is None
    assert rebuilds[0].model_provider == "ollama"
    output = "\n".join(str(message) for message in console.messages)
    assert "llama3.2" in output
    assert "history kept" in output


async def test_model_command_sets_provider_and_model():
    settings = _FakeModelSettings(model_provider="anthropic")

    agent, new_settings, console, _ = await _run_model_command(
        "ollama qwen3", settings, lambda s: _SwitchedAgent()
    )

    assert isinstance(agent, _SwitchedAgent)
    assert new_settings.model_provider == "ollama"
    assert new_settings.model == "qwen3"


async def test_model_command_changes_model_on_current_provider():
    settings = _FakeModelSettings(
        model_provider="anthropic",
        provider_models={"anthropic": "claude-sonnet-5"},
    )

    agent, new_settings, console, _ = await _run_model_command(
        "my-custom-model", settings, lambda s: _SwitchedAgent()
    )

    assert isinstance(agent, _SwitchedAgent)
    assert new_settings.model_provider == "anthropic"
    assert new_settings.model == "my-custom-model"


async def test_model_command_rejects_provider_without_configured_model():
    settings = _FakeModelSettings(
        model_provider="anthropic",
        provider_models={"anthropic": "claude-sonnet-5"},
    )

    def rebuild(new_settings):
        # build_model raises this when neither config nor spec supplies one.
        raise RuntimeError(
            "Vikram model is not configured. Run `vikram configure` or set "
            "VIKRAM_MODEL."
        )

    agent, new_settings, console, old_agent = await _run_model_command(
        "gemini", settings, rebuild
    )

    assert agent is old_agent
    assert new_settings is settings
    output = "\n".join(str(message) for message in console.messages)
    assert "No model configured for gemini" in output


async def test_model_command_keeps_agent_when_rebuild_fails():
    settings = _FakeModelSettings(
        model_provider="anthropic",
        provider_models={"anthropic": "claude-sonnet-5", "gemini": "gemini-2.5-flash"},
    )

    def rebuild(new_settings):
        raise RuntimeError("GEMINI_API_KEY is not set.")

    agent, new_settings, console, old_agent = await _run_model_command(
        "gemini", settings, rebuild
    )

    assert agent is old_agent
    assert new_settings is settings
    output = "\n".join(str(message) for message in console.messages)
    assert "GEMINI_API_KEY" in output


async def test_model_command_suggests_provider_for_typo():
    settings = _FakeModelSettings(
        model_provider="ollama",
        provider_models={"ollama": "llama3.2"},
    )
    rebuilds = []

    agent, new_settings, console, old_agent = await _run_model_command(
        "anthropc", settings, lambda s: rebuilds.append(s)
    )

    assert agent is old_agent
    assert rebuilds == []
    output = "\n".join(str(message) for message in console.messages)
    assert "/model anthropic" in output


async def test_model_command_unavailable_without_rebuild_callback():
    agent, new_settings, console, old_agent = await _run_model_command(
        "ollama", _FakeModelSettings(), None
    )

    assert agent is old_agent
    output = "\n".join(str(message) for message in console.messages)
    assert "not available" in output


async def test_run_interactive_routes_model_command(monkeypatch, tmp_path):
    import prompt_toolkit
    import prompt_toolkit.history
    import rich.console

    from vikram import cli
    from vikram.cli import run_interactive

    prompts = []

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def prompt_async(self, prompt, **kwargs):
            prompts.append(prompt)
            if len(prompts) == 1:
                return "/model ollama"
            raise EOFError

    class SilentConsole:
        def print(self, *args, **kwargs):
            pass

    monkeypatch.setattr(prompt_toolkit, "PromptSession", FakeSession)
    monkeypatch.setattr(prompt_toolkit.history, "FileHistory", lambda *a, **k: None)
    monkeypatch.setattr(rich.console, "Console", SilentConsole)
    monkeypatch.setattr(cli, "HISTORY_PATH", tmp_path / "hist")

    settings = _FakeModelSettings(provider_models={"ollama": "llama3.2"})
    rebuilds = []

    def rebuild(new_settings):
        rebuilds.append(new_settings)
        return _SwitchedAgent()

    await run_interactive(
        SimpleNamespace(),
        prog_name="Demo",
        quiet=False,
        settings=settings,
        rebuild_agent=rebuild,
    )

    assert len(rebuilds) == 1
    assert rebuilds[0].model_provider == "ollama"


async def test_model_command_selector_picks_by_number():
    settings = _FakeModelSettings(
        model_provider="anthropic",
        provider_models={"ollama": "llama3.2", "anthropic": "claude-sonnet-5"},
    )
    rebuilds = []

    def rebuild(new_settings):
        rebuilds.append(new_settings)
        return _SwitchedAgent()

    async def pick(prompt):
        return "1"  # registry order: ollama is the first configured entry

    agent, new_settings, console, _ = await _run_model_command(
        "", settings, rebuild, input_async=pick
    )

    assert isinstance(agent, _SwitchedAgent)
    assert rebuilds[0].model_provider == "ollama"
    # The picked row's displayed model is selected explicitly, so spec pins
    # cannot swap in a different model than the one shown.
    assert rebuilds[0].model == "llama3.2"
    output = "\n".join(str(message) for message in console.messages)
    assert "1) ollama" in output
    assert "2) anthropic" in output


async def test_model_command_selector_blank_cancels():
    settings = _FakeModelSettings(
        model_provider="anthropic",
        provider_models={"anthropic": "claude-sonnet-5"},
    )
    rebuilds = []

    async def pick(prompt):
        return ""

    agent, new_settings, console, old_agent = await _run_model_command(
        "", settings, rebuild=lambda s: rebuilds.append(s), input_async=pick
    )

    assert agent is old_agent
    assert rebuilds == []
    output = "\n".join(str(message) for message in console.messages)
    assert "Cancelled" in output


async def test_model_command_persists_agent_choice(monkeypatch, tmp_path):
    import tomllib as _tomllib

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    settings = _FakeModelSettings(
        model_provider="anthropic",
        provider_models={"ollama": "llama3.2", "anthropic": "claude-sonnet-5"},
    )

    class _OllamaAgent:
        model_config = {"provider": "ollama", "model": "llama3.2"}

    agent, new_settings, console, _ = await _run_model_command(
        "ollama", settings, lambda s: _OllamaAgent(), agent_id="coder"
    )

    config_file = tmp_path / "vikram" / "config.toml"
    data = _tomllib.loads(config_file.read_text(encoding="utf-8"))
    assert data["agents"]["coder"] == {"provider": "ollama", "model": "llama3.2"}
    assert new_settings.agent_overrides["coder"] == {
        "provider": "ollama",
        "model": "llama3.2",
    }
    output = "\n".join(str(message) for message in console.messages)
    assert "saved as the coder default" in output
