import contextlib
import io
import unittest
from unittest.mock import patch

from test_toolkit import fake_vm

from vikram import cli


def setUpModule():
    # Replies get a fake sandbox here; test_toolkit covers the real VM.
    unittest.enterModuleContext(fake_vm())


class CliOptionsTests(unittest.TestCase):
    def test_removed_web_flag_is_rejected_before_starting_chat(self):
        for mode in ("auto", "off"):
            with (
                self.subTest(mode=mode),
                contextlib.redirect_stderr(io.StringIO()),
                patch("vikram.cli._chat") as chat,
            ):
                with self.assertRaises(SystemExit) as result:
                    cli.main(["--web", mode, "Hello"])
                self.assertEqual(result.exception.code, 2)
                chat.assert_not_called()

    def test_legacy_web_commands_do_not_send_messages_or_claim_to_disable_tools(self):
        output = io.StringIO()
        with (
            patch(
                "builtins.input", side_effect=["/web off", "/web auto", "/web", "/exit"]
            ),
            patch("vikram.cli._reply") as reply,
            contextlib.redirect_stderr(output),
        ):
            self.assertEqual(cli.main([]), 0)
        reply.assert_not_called()
        self.assertNotIn("Web Off", output.getvalue())
        self.assertIn("no web setting", output.getvalue().lower())


class ThinkingDefaultTests(unittest.TestCase):
    """The owner's rule: thinking is on unless explicitly turned off."""

    def model(self, values, default=None):
        return {
            "name": "local:1",
            "available": True,
            "tools": True,
            "thinking": values,
            "thinking_default": default,
        }

    def test_default_is_on_and_off_must_be_explicit(self):
        from vikram.models import prepare_model, thinking_selection

        models = [self.model([False, True], True)]
        for choice in ("on", "default"):
            prepared = prepare_model(models, "local:1", thinking_selection(choice))
            self.assertEqual((prepared.setting, prepared.thinking), (None, True))
        prepared = prepare_model(models, "local:1", thinking_selection("off"))
        self.assertEqual((prepared.setting, prepared.thinking), (False, False))
        self.assertIsNone(prepared.notice)

    def test_on_uses_the_model_level_and_unsupported_off_stays_on(self):
        from vikram.models import prepare_model
        from vikram.ollama import thinking_on

        self.assertEqual(thinking_on(["low", "high"], "high"), "high")
        self.assertEqual(thinking_on(["low", "high"]), "low")
        self.assertEqual(thinking_on([False, True, "medium"], True), True)
        self.assertIsNone(thinking_on([]))
        prepared = prepare_model([self.model([True], True)], "local:1", False)
        self.assertEqual((prepared.setting, prepared.thinking), (None, True))
        self.assertIn("Using thinking on", prepared.notice)
        prepared = prepare_model([self.model([])], "local:1", None)
        self.assertIsNone(prepared.thinking)

    def test_cli_defaults_to_on(self):
        with patch("vikram.cli._chat") as chat:
            self.assertEqual(cli.main(["Hello"]), 0)
        self.assertEqual(chat.call_args.kwargs["thinking"], "on")


class PlainOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_model_output_cannot_emit_terminal_controls(self):
        class Session:
            async def reply(self, prompt, **kwargs):
                yield {"text": "hello\x1b]52;c;hidden\x07\x1b[31mworld", "sources": []}

        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            patch("vikram.cli._interaction", return_value=None),
        ):
            await cli._reply(Session(), "hi")
        self.assertEqual(output.getvalue(), "helloworld\n")

    async def test_plain_setting_selects_plain_loop(self):
        import os
        import tempfile
        from pathlib import Path

        from vikram.settings import Settings

        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(Path(directory))
            settings.set("interface", "plain")
            with (
                patch.object(cli.sys.stdin, "isatty", return_value=True),
                patch.object(cli.sys.stdout, "isatty", return_value=True),
                patch.dict(os.environ, {"TERM": "xterm"}),
                patch("vikram.cli._loop") as loop,
                patch("vikram.tui.TerminalApp") as tui,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                # _chat owns its synchronous Runner, so run it outside this test loop.
                import asyncio

                await asyncio.to_thread(cli.main, ["--session-dir", directory])
            loop.assert_called_once()
            tui.assert_not_called()


class SettingsPrecedenceTests(unittest.TestCase):
    def test_explicit_thinking_overrides_saved_new_session_preference(self):
        import tempfile
        from pathlib import Path

        from vikram.settings import Settings

        with tempfile.TemporaryDirectory() as directory:
            Settings(Path(directory)).set("thinking", "off")
            with patch("vikram.cli._chat") as chat:
                cli.main(["--session-dir", directory, "Hello"])
                self.assertEqual(chat.call_args.kwargs["thinking"], "off")
                cli.main(["--session-dir", directory, "--thinking", "on", "Hello"])
                self.assertEqual(chat.call_args.kwargs["thinking"], "on")


class ModelDefaultPrecedenceTests(unittest.TestCase):
    def test_explicit_cli_and_environment_override_shared_default_without_replacing_it(
        self,
    ):
        import os
        import tempfile
        from pathlib import Path

        from vikram.settings import Settings

        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(root=Path(folder))
            settings.set("model", "saved:1")
            with (
                patch.dict(os.environ, {"OLLAMA_MODEL": "environment:1"}),
                patch("vikram.cli._chat") as chat,
            ):
                cli.main(["--session-dir", folder, "Hello"])
                self.assertEqual(chat.call_args.args[0], "environment:1")
                cli.main(["--session-dir", folder, "--model", "explicit:1", "Hello"])
                self.assertEqual(chat.call_args.args[0], "explicit:1")
            self.assertEqual(settings.get("model"), "saved:1")

    def test_explicitly_saved_cloud_default_is_restored(self):
        import os
        import tempfile
        from pathlib import Path

        from vikram.settings import Settings

        with tempfile.TemporaryDirectory() as folder:
            Settings(root=Path(folder)).set("model", "openrouter:example/model")
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("vikram.cli._chat") as chat,
            ):
                cli.main(["--session-dir", folder, "Hello"])
            self.assertEqual(chat.call_args.args[0], "openrouter:example/model")
