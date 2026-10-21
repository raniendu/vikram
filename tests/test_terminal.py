import asyncio
import contextlib
import io
import unittest
from unittest.mock import AsyncMock, patch

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from pydantic_ai_harness.ask_user import AskUserRequest, Question
from rich.console import Console
from test_toolkit import fake_vm

from vikram.cli import ChatSession, _reply
from vikram.interaction import PlanRequest
from vikram.terminal import (
    TerminalChat,
    TerminalInteraction,
    render_reply,
    run_terminal,
    terminal_text,
)

QUESTION = AskUserRequest(
    questions=(
        Question(
            header="Scale",
            question="Which scale is 25 degrees in?",
            options=({"label": "Celsius"}, {"label": "Fahrenheit"}),
        ),
    )
)


def setUpModule():
    # Replies get a fake sandbox here; test_toolkit covers the real VM.
    unittest.enterModuleContext(fake_vm())


async def models():
    return [{"name": "local:1", "available": True, "tools": True, "thinking": []}]


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


class TerminalPromptTests(unittest.IsolatedAsyncioTestCase):
    async def typed(self, text, call):
        output = io.StringIO()
        with create_pipe_input() as keyboard:
            prompter = TerminalInteraction(
                Console(file=output, width=80),
                terminal_input=keyboard,
                output=DummyOutput(),
            )
            keyboard.send_text(text)
            async with asyncio.timeout(3):
                return await call(prompter), output.getvalue()

    async def test_numbers_pick_options_and_words_become_the_answer(self):
        response, shown = await self.typed("9\r2\r", lambda p: p.ask(QUESTION))
        self.assertEqual(response.answers[0].selected, ("Fahrenheit",))
        self.assertIn("Which scale is 25 degrees in?", shown)
        self.assertIn("Choose listed numbers", shown)
        response, _ = await self.typed("It is Kelvin\r", lambda p: p.ask(QUESTION))
        self.assertEqual(response.answers[0].custom_answer, "It is Kelvin")
        response, _ = await self.typed("\r", lambda p: p.ask(QUESTION))
        self.assertTrue(response.cancelled)

    async def test_plan_review_choices(self):
        review, shown = await self.typed(
            "y\r", lambda p: p.review_plan(PlanRequest(id="p", plan="1. Compute"))
        )
        self.assertEqual(review.decision, "approve")
        self.assertIn("Compute", shown)
        review, _ = await self.typed(
            "c\rAdd a check\r",
            lambda p: p.review_plan(PlanRequest(id="p", plan="1. Compute")),
        )
        self.assertEqual((review.decision, review.feedback), ("revise", "Add a check"))
        review, _ = await self.typed(
            "\r", lambda p: p.review_plan(PlanRequest(id="p", plan="1. Compute"))
        )
        self.assertEqual(review.decision, "dismiss")

    async def test_browser_approval_choices(self):
        from vikram.interaction import ActionRequest

        request = ActionRequest(
            id="b1",
            summary="Click “Place order” on shop.example",
            reason="It looks like it buys or pays for something.",
            url="https://shop.example/cart",
        )
        allowed, shown = await self.typed("maybe\ry\r", lambda p: p.approve(request))
        self.assertTrue(allowed)
        self.assertIn("Place order", shown)
        self.assertIn("Type y or n.", shown)
        self.assertFalse((await self.typed("\r", lambda p: p.approve(request)))[0])
        self.assertFalse((await self.typed("n\r", lambda p: p.approve(request)))[0])

    async def test_reply_pauses_for_a_browser_approval_and_resumes(self):
        from vikram.interaction import ActionRequest

        request = ActionRequest(
            id="b1",
            summary="Click “Submit order” on httpbin.org",
            reason="It submits a form.",
            url="https://httpbin.org/forms/post",
        )

        async def stream(*args, interaction, mode, **kwargs):
            yield {"text": "", "phase": "approving", "mode": mode, "activity": []}
            allowed = await interaction.approve(request)
            record = {
                "kind": "browser",
                "id": "b1",
                "action": "click",
                "status": "complete" if allowed else "denied",
                "target": "button “Submit order”",
                "text": None,
                "url": "https://httpbin.org/post",
                "title": "",
                "error": None,
                "approval": None,
            }
            yield {
                "text": "Submitted." if allowed else "Not submitted.",
                "phase": "answering",
                "mode": mode,
                "activity": [record],
            }

        session = ChatSession("local:1", streamer=stream)
        session.catalog.refresh = models
        output = io.StringIO()
        with create_pipe_input() as keyboard:
            terminal = TerminalChat(
                session,
                console=Console(file=output, width=80),
                terminal_input=keyboard,
                terminal_output=DummyOutput(),
            )
            task = asyncio.create_task(terminal.answer("Order it"))
            async with asyncio.timeout(3):
                while "allow this action" not in output.getvalue():
                    await asyncio.sleep(0.01)
                keyboard.send_text("y\r")
                await task
            terminal.show_activity()
        shown = output.getvalue()
        self.assertIn("Submitted.", shown)
        self.assertIn("Browser · 1 action", shown)
        self.assertIn("Browser · complete · Clicked button “Submit order”", shown)

    async def test_reply_pauses_for_a_question_and_resumes(self):
        async def stream(*args, interaction, mode, **kwargs):
            yield {"text": "", "phase": "asking", "mode": mode, "activity": []}
            response = await interaction.ask(QUESTION)
            label = response.answers[0].selected[0]
            yield {
                "text": f"Using {label}.",
                "phase": "answering",
                "mode": "normal",
                "activity": [
                    {
                        "kind": "question",
                        "id": "q",
                        "status": "answered",
                        "questions": [q.model_dump() for q in QUESTION.questions],
                        "answers": {"Scale": [label]},
                    },
                    {
                        "kind": "code",
                        "id": "c",
                        "status": "complete",
                        "code": "1 + 1",
                        "output": "2",
                        "error": None,
                    },
                ],
            }

        session = ChatSession("local:1", streamer=stream, mode="plan")
        session.catalog.refresh = models
        output = io.StringIO()
        with create_pipe_input() as keyboard:
            terminal = TerminalChat(
                session,
                console=Console(file=output, width=80),
                terminal_input=keyboard,
                terminal_output=DummyOutput(),
            )
            task = asyncio.create_task(terminal.answer("Convert 25 degrees"))
            async with asyncio.timeout(3):
                while "Vikram asks" not in output.getvalue():
                    await asyncio.sleep(0.01)
                keyboard.send_text("2\r")
                await task
            terminal.show_activity()
        shown = output.getvalue()
        self.assertIn("Using Fahrenheit.", shown)
        self.assertIn("Question · answered", shown)
        self.assertIn("Sandbox · 1 run", shown)
        self.assertIn("Plan approved", shown)
        self.assertIn("1 + 1", shown)
        self.assertEqual(session.mode, "normal")
        self.assertEqual(session.turns[-1]["answer"], "Using Fahrenheit.")
        self.assertEqual(
            session.turns[-1]["activity"][0]["answers"], {"Scale": ["Fahrenheit"]}
        )

    async def test_ctrl_c_at_a_question_stops_the_reply(self):
        closed = asyncio.Event()

        async def stream(*args, interaction, **kwargs):
            try:
                yield {"text": "", "phase": "asking", "activity": []}
                await interaction.ask(QUESTION)
                yield {"text": "Never", "phase": "answering", "activity": []}
            finally:
                closed.set()

        session = ChatSession("local:1", streamer=stream)
        session.catalog.refresh = models
        output = io.StringIO()
        with create_pipe_input() as keyboard:
            terminal = TerminalChat(
                session,
                console=Console(file=output, width=80),
                terminal_input=keyboard,
                terminal_output=DummyOutput(),
            )
            task = asyncio.create_task(terminal.answer("Convert"))
            async with asyncio.timeout(3):
                while "Vikram asks" not in output.getvalue():
                    await asyncio.sleep(0.01)
                keyboard.send_bytes(b"\x03")
                await task
        self.assertTrue(closed.is_set())
        self.assertIn("Stopped", output.getvalue())
        self.assertIsNone(session.turns[-1]["answer"])

    def test_plan_toggle_and_activity_summary(self):
        output = io.StringIO()
        terminal = TerminalChat(
            ChatSession("local:1"), console=Console(file=output, width=80)
        )
        terminal.toggle_plan()
        self.assertEqual(terminal.session.mode, "plan")
        terminal.toggle_plan()
        self.assertEqual(terminal.session.mode, "normal")
        self.assertIn("Plan mode on", output.getvalue())
        shown = render(
            {
                "text": "Done",
                "activity": [
                    {"kind": "plan", "status": "not_approved", "plan": "1. Go"},
                    {"kind": "code", "status": "failed", "code": "x", "output": ""},
                ],
            },
            status="complete",
        )
        self.assertIn("Sandbox · 1 run · 1 failed", shown)
        self.assertIn("Plan · not approved", shown)
        actions = [
            {"kind": "browser", "action": "open", "status": "complete"},
            {"kind": "browser", "action": "click", "status": "denied"},
        ]
        shown = render({"text": "Done", "activity": actions}, status="complete")
        self.assertIn("Browser · 2 actions · 1 not allowed", shown)

    def test_plain_cli_browser_lines(self):
        from vikram.cli import browser_line

        record = {"kind": "browser", "action": "open", "target": "https://a.example/"}
        self.assertEqual(
            browser_line({**record, "status": "running"}),
            "Browser: Opening https://a.example/",
        )
        self.assertIsNone(browser_line({**record, "status": "complete"}))
        self.assertEqual(
            browser_line(
                {
                    **record,
                    "status": "denied",
                    "approval": {"summary": "Click “Buy”", "reason": "Buys"},
                }
            ),
            "Browser: not allowed: Click “Buy”",
        )
        self.assertEqual(
            browser_line({**record, "status": "refused", "error": "No passwords.\nx"}),
            "Browser: No passwords.",
        )


if __name__ == "__main__":
    unittest.main()
