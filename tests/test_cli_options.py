import contextlib
import io
import unittest
from unittest.mock import patch

from vikram import cli


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
