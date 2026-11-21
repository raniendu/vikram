"""Context usage is the model's own count for the latest request, not a guess."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.usage import RequestUsage
from test_desktop_runtime import ENTRY, SHOW
from test_toolkit import fake_vm, sse, tool_call

from vikram.conversation import context_tokens, stream_reply
from vikram.desktop.models import parse_model
from vikram.desktop.runtime import Runtime
from vikram.desktop.store import Store
from vikram.session import Session

MODEL = {
    "name": "local:1",
    "available": True,
    "vision": False,
    "tools": False,
    "thinking": [],
    "thinking_default": None,
    "context_length": 10_000,
}


def setUpModule():
    unittest.enterModuleContext(fake_vm())


def reported(prompt_tokens, completion_tokens, delta):
    """One streamed completion that ends with the server's token counts."""

    async def respond(request):
        finish = "tool_calls" if "tool_calls" in delta else "stop"
        chunk = {
            "id": "r",
            "model": "local:1",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        usage = {
            "id": "r",
            "model": "local:1",
            "choices": [],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=sse(chunk) + sse(usage) + "data: [DONE]\n\n",
        )

    return respond


class ContextTokensTests(unittest.IsolatedAsyncioTestCase):
    def test_latest_response_counts_not_the_run_total(self):
        messages = [
            ModelRequest(parts=[UserPromptPart("Go")]),
            ModelResponse(
                parts=[TextPart("a")],
                usage=RequestUsage(input_tokens=5_000, output_tokens=40),
            ),
            ModelRequest(parts=[UserPromptPart("more")]),
            ModelResponse(
                parts=[TextPart("b")],
                usage=RequestUsage(input_tokens=6_200, output_tokens=300),
            ),
        ]
        self.assertEqual(context_tokens(messages), 6_500)
        self.assertIsNone(context_tokens([ModelResponse(parts=[TextPart("x")])]))
        self.assertIsNone(context_tokens([]))

    async def test_stream_reports_latest_request_beside_run_totals(self):
        # A retried unknown tool makes two requests without running anything.
        steps = [
            reported(5_081, 30, tool_call("no_such_tool", {})),
            reported(6_228, 372, {"content": "Done"}),
        ]

        async def respond(request):
            return await steps.pop(0)(request)

        events = [
            event
            async for event in stream_reply(
                "local:1",
                False,
                [],
                ["Go"],
                web="auto",
                session_id="a" * 32,
                transport=httpx.MockTransport(respond),
            )
        ]
        usage = next(e["usage"] for e in reversed(events) if e.get("usage"))
        self.assertEqual(usage["requests"], 2)
        self.assertEqual(usage["input_tokens"], 5_081 + 6_228)
        self.assertEqual(usage["context_tokens"], 6_228 + 372)


class SessionContextTests(unittest.IsolatedAsyncioTestCase):
    def session(self, turns, *, limit=262_144):
        session = Session("local:1")
        session.turns = turns
        session.context_limit = limit
        return session

    def test_reported_count_includes_what_saved_text_cannot_see(self):
        turns = [
            {
                "id": "m1",
                "prompt": ["Pick a pizza size"],
                "answer": "Small, medium or large?",
                "usage": {"input_tokens": 9_000, "context_tokens": 5_247},
            },
            {"id": "m2", "prompt": ["x" * 400], "answer": None},
        ]
        state = self.session(turns).context_state()
        self.assertFalse(state["estimated"])
        # The reported prompt plus a rough count of the unsent turn after it
        # (400 characters, and 4 for its empty sources and activity).
        self.assertEqual(state["tokens"], 5_247 + 101)
        self.assertLess(state["estimated_tokens"], 200)
        self.assertAlmostEqual(state["percent"], 5_348 / 262_144 * 100)

    def test_compaction_sets_older_reports_aside(self):
        turns = [
            {
                "id": f"m{i}",
                "prompt": ["q" * 400],
                "answer": "a" * 400,
                "usage": {"context_tokens": 90_000, "summary_through": None},
            }
            for i in range(4)
        ]
        session = self.session(turns, limit=100_000)
        self.assertEqual(session.context_state()["tokens"], 90_000)
        session.summary, session._summary_count = "s" * 400, 2
        state = session.context_state()
        self.assertTrue(state["estimated"])
        self.assertEqual(state["tokens"], 100 + 2 * 201)
        turns[-1]["usage"] = {"context_tokens": 7_000, "summary_through": "m1"}
        self.assertEqual(session.context_state()["tokens"], 7_000)
        self.assertEqual(session.summary_marker(), "m1")

    def test_no_report_falls_back_to_the_text_estimate(self):
        state = self.session(
            [{"prompt": ["x" * 4_000], "answer": "", "usage": {"input_tokens": 9}}]
        ).context_state()
        self.assertTrue(state["estimated"])
        self.assertEqual(state["tokens"], 1_001)

    async def test_cli_reply_records_and_compacts_on_the_reported_count(self):
        async def stream(model, thinking, history, prompt, **kwargs):
            yield {
                "text": "short",
                "actual_model": model,
                "usage": {"input_tokens": 9_000, "context_tokens": 8_500},
            }

        summarizer = AsyncMock(return_value="memory")
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            session = Session(
                "local:1", store=store, streamer=stream, summarizer=summarizer
            )
            session.catalog.refresh = AsyncMock(return_value=[MODEL])
            for prompt in ("one", "two", "three"):
                async for _ in session.reply(prompt):
                    pass
                if prompt == "one":
                    state = session.context_state()
                    self.assertFalse(state["estimated"])
                    self.assertEqual(state["tokens"], 8_500)
                    self.assertEqual(state["percent"], 85)
            # The saved text is tiny; the reported 85% is what forced compaction.
            summarizer.assert_awaited()
            self.assertEqual(session.summary, "memory")
            saved = store.chat(session.session_id)["messages"][-1]["attempts"][0]
            self.assertEqual(
                saved["usage"]["summary_through"], session.summary_marker()
            )
            await session.close()
            store.close()


class DesktopContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_desktop_reply_records_which_compaction_it_used(self):
        async def stream(model, thinking, history, prompt, **kwargs):
            yield {
                "text": "answer",
                "phase": "answering",
                "usage": {"input_tokens": 12, "context_tokens": 4_000},
            }

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            chat = store.create_chat("local:1")
            runtime = Runtime(store, lambda event: None, streamer=stream)
            model = {**parse_model(ENTRY, SHOW), "context_length": 32_768}
            runtime.catalog.refresh = AsyncMock(return_value=[model])
            runtime.models = [model]
            try:
                store.save_draft(chat["id"], "Hello", 1)
                await runtime.send(chat["id"], "request")
                await runtime.task
                attempt = store.chat(chat["id"])["messages"][0]["attempts"][0]
                self.assertEqual(
                    attempt["usage"], {**attempt["usage"], "summary_through": None}
                )
                state = runtime.workbench.context(chat["id"]).context_state()
                self.assertEqual(state["tokens"], 4_000)
                self.assertEqual(state["context_limit"], 32_768)
                self.assertFalse(state["estimated"])
            finally:
                await runtime.stop()
                await runtime.close()
                store.close()


if __name__ == "__main__":
    unittest.main()
