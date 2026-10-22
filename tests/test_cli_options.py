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
