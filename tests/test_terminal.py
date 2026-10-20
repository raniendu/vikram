import asyncio
import contextlib
import io
import unittest
from unittest.mock import AsyncMock, patch

from prompt_toolkit.input import create_pipe_input
from rich.console import Console

from vikram.cli import ChatSession, _reply
from vikram.terminal import TerminalChat, render_reply, run_terminal, terminal_text


def render(event, **options):
    output = io.StringIO()
    Console(file=output, width=80, color_system=None).print(
        render_reply(event, **options)
    )
    return output.getvalue()


class TerminalRenderingTests(unittest.TestCase):
    def test_terminal_startup_closes_non_context_manager_input(self):
        from prompt_toolkit.input import DummyInput

        keyboard = DummyInput()
        with (
            patch("vikram.terminal.create_input", return_value=keyboard),
            patch.object(keyboard, "close") as close,
            patch.object(TerminalChat, "run", new_callable=AsyncMock) as run,
        ):
            run_terminal(ChatSession("local:1"))
        run.assert_awaited_once()
        close.assert_called_once()

    def test_thinking_is_opt_in_and_keeps_answer_separate(self):
        event = {
            "text": "**Answer**",
            "reasoning": "Model reasoning",
            "phase": "answering",
        }
        collapsed = render(event)
        self.assertIn("Answer", collapsed)
        self.assertIn("Thinking", collapsed)
        self.assertNotIn("Model reasoning", collapsed)
        expanded = render(event, show_thinking=True)
        self.assertIn("Model reasoning", expanded)
        self.assertIn("Answer", expanded)
        self.assertNotIn("**Answer**", expanded)

    def test_model_output_cannot_inject_terminal_control_sequences(self):
        value = "before\x1b[2Jafter\x1b]52;c;Y2xpcGJvYXJk\x07\x07\r\x9b2J\nnext"
        safe = terminal_text(value)
        for control in ("\x1b", "\x07", "\r", "\x9b"):
            self.assertNotIn(control, safe)
        self.assertIn("beforeafter", safe)
        self.assertIn("\nnext", safe)


class TerminalInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_toggle_and_stop_close_stream_without_saving_partial_as_answer(self):
        started, closed = asyncio.Event(), asyncio.Event()

        async def stream(*args, **kwargs):
            try:
                yield {
                    "text": "Partial answer",
                    "reasoning": "Model reasoning",
                    "phase": "thinking",
                }
                started.set()
                await asyncio.Event().wait()
            finally:
                closed.set()

        session = ChatSession("local:1", streamer=stream)

        async def models():
            return [
                {"name": "local:1", "available": True, "tools": True, "thinking": []}
            ]

        session.catalog.refresh = models
        output = io.StringIO()
        with create_pipe_input() as keyboard:
            terminal = TerminalChat(
                session, console=Console(file=output, width=80), terminal_input=keyboard
            )
            task = asyncio.create_task(terminal.answer("Hello"))
            async with asyncio.timeout(3):
                await started.wait()
                keyboard.send_bytes(b"\x14")
                while not terminal.show_thinking:
                    await asyncio.sleep(0.01)
                keyboard.send_bytes(b"\x03")
                await task
            self.assertTrue(closed.is_set())
            self.assertIsNone(session.turns[-1]["answer"])
            self.assertNotIn("reasoning", session.turns[-1])
            self.assertEqual(terminal.last_event["reasoning"], "Model reasoning")
            self.assertIn("Stopped", output.getvalue())

    async def test_plain_cli_keeps_thinking_out_of_stdout(self):
        async def stream(*args, **kwargs):
            yield {
                "text": "Answer",
                "reasoning": "Model reasoning",
                "phase": "answering",
            }

        session = ChatSession("local:1", streamer=stream)

        async def models():
            return [
                {"name": "local:1", "available": True, "tools": False, "thinking": []}
            ]

        session.catalog.refresh = models
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            await _reply(session, "Hello")
        self.assertEqual(output.getvalue(), "Answer\n")


if __name__ == "__main__":
    unittest.main()
