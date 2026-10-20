import asyncio
import json
import unittest
from unittest.mock import patch

import httpx
from pydantic_ai.messages import ModelRequest, UserPromptPart

from vikram import conversation
from vikram.web import WebSession


def sse(payload):
    return "data: " + json.dumps(payload) + "\n\n"


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_reasoning_streams_separately_from_the_answer(self):
        async def handler(request):
            chunks = ""
            for delta in (
                {"reasoning": "Private thinking"},
                {"reasoning": " continues"},
                {"content": "Today is Thursday."},
            ):
                chunks += sse(
                    {
                        "id": "r",
                        "model": "reported:1",
                        "choices": [{"index": 0, "delta": delta}],
                    }
                )
            chunks += (
                sse(
                    {
                        "id": "r",
                        "model": "reported:1",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                )
                + "data: [DONE]\n\n"
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=httpx.ByteStream(chunks.encode()),
            )

        events = [
            e
            async for e in conversation.stream_reply(
                "local:1",
                None,
                [],
                "What date is today?",
                transport=httpx.MockTransport(handler),
            )
        ]
        self.assertEqual(events[0]["phase"], "thinking")
        self.assertEqual(events[0]["actual_model"], "reported:1")
        self.assertEqual(events[0]["text"], "")
        self.assertEqual(events[-1]["reasoning"], "Private thinking continues")
        self.assertFalse(events[-1]["reasoning_truncated"])
        self.assertTrue(all("Private thinking" not in e["text"] for e in events))
        self.assertEqual(events[-1]["text"], "Today is Thursday.")

    async def test_long_thinking_is_bounded_without_losing_the_answer(self):
        def handler(request):
            chunks = (
                "".join(
                    sse(
                        {
                            "id": "r",
                            "model": "local:1",
                            "choices": [
                                {"index": 0, "delta": delta, "finish_reason": None}
                            ],
                        }
                    )
                    for delta in ({"reasoning": "x" * 70_000}, {"content": "Answer"})
                )
                + sse(
                    {
                        "id": "r",
                        "model": "local:1",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                )
                + "data: [DONE]\n\n"
            )
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, text=chunks
            )

        events = [
            e
            async for e in conversation.stream_reply(
                "local:1", None, [], "Hello", transport=httpx.MockTransport(handler)
            )
        ]
        self.assertEqual(events[-1]["reasoning"], "x" * 64_000)
        self.assertTrue(events[-1]["reasoning_truncated"])
        self.assertEqual(events[-1]["text"], "Answer")

    async def test_tool_budget_leaves_a_final_answer_request_without_tools(self):
        requests, calls = [], []

        async def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            delta = {"content": "Final answer"}
            if body.get("tools"):
                delta = {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"c{len(requests)}",
                            "type": "function",
                            "function": {
                                "name": "web_search",
                                "arguments": json.dumps(
                                    {"objective": "Docs", "search_queries": ["docs"]}
                                ),
                            },
                        }
                    ]
                }
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=sse(
                    {
                        "id": "r",
                        "model": "local:1",
                        "choices": [
                            {
                                "index": 0,
                                "delta": delta,
                                "finish_reason": (
                                    "tool_calls" if body.get("tools") else "stop"
                                ),
                            }
                        ],
                    }
                )
                + "data: [DONE]\n\n",
            )

        async def remote(name, args):
            calls.append(name)
            return {"results": []}

        with patch(
            "vikram.conversation.WebSession",
            side_effect=lambda sid, **kwargs: WebSession(sid, remote=remote, **kwargs),
        ):
            events = [
                e
                async for e in conversation.stream_reply(
                    "local:1",
                    False,
                    [],
                    ["Find docs"],
                    web="auto",
                    transport=httpx.MockTransport(handler),
                )
            ]
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(requests), 4)
        self.assertFalse(requests[-1].get("tools"))
        self.assertEqual(events[-1]["text"], "Final answer")

    async def test_closing_shared_stream_cancels_inflight_remote_call(self):
        started, closed = asyncio.Event(), asyncio.Event()

        async def handler(request):
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": json.dumps(
                                {"objective": "Docs", "search_queries": ["docs"]}
                            ),
                        },
                    }
                ]
            }
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=sse(
                    {
                        "id": "r",
                        "model": "local:1",
                        "choices": [
                            {"index": 0, "delta": delta, "finish_reason": "tool_calls"}
                        ],
                    }
                )
                + "data: [DONE]\n\n",
            )

        async def remote(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

        with patch(
            "vikram.conversation.WebSession",
            side_effect=lambda sid, **kwargs: WebSession(sid, remote=remote, **kwargs),
        ):
            stream = conversation.stream_reply(
                "local:1",
                False,
                [],
                ["Find docs"],
                web="auto",
                transport=httpx.MockTransport(handler),
            )
            async with asyncio.timeout(2):
                async for event in stream:
                    if event["phase"] == "searching":
                        break
                await started.wait()
                await stream.aclose()
            self.assertTrue(closed.is_set())

    async def test_text_then_tool_continues_to_final_answer(self):
        requests = []

        async def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            delta = (
                {
                    "content": "Checking…",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "web_search",
                                "arguments": json.dumps(
                                    {
                                        "objective": "Find docs",
                                        "search_queries": ["official docs"],
                                    }
                                ),
                            },
                        }
                    ],
                }
                if len(requests) == 1
                else {"content": "Answer with https://example.org/docs"}
            )
            chunks = sse(
                {
                    "id": "r",
                    "model": "local:1",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"reasoning": f"Thinking round {len(requests)}"},
                        }
                    ],
                }
            ) + sse(
                {
                    "id": "r",
                    "model": "local:1",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                }
            )
            chunks += (
                sse(
                    {
                        "id": "r",
                        "model": "local:1",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": (
                                    "tool_calls" if len(requests) == 1 else "stop"
                                ),
                            }
                        ],
                    }
                )
                + "data: [DONE]\n\n"
            )
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, text=chunks
            )

        async def remote(name, args):
            return {
                "results": [
                    {
                        "url": "https://example.org/docs",
                        "title": "Docs",
                        "excerpts": ["Evidence"],
                    }
                ]
            }

        with patch(
            "vikram.conversation.WebSession", wraps=conversation.WebSession
        ) as session:
            session.side_effect = lambda sid, **kwargs: WebSession(
                sid, remote=remote, **kwargs
            )
            events = [
                e
                async for e in conversation.stream_reply(
                    "local:1",
                    False,
                    [],
                    ["Find current docs"],
                    web="auto",
                    session_id="a" * 32,
                    transport=httpx.MockTransport(handler),
                )
            ]
        self.assertEqual(len(requests), 2)
        self.assertEqual(
            events[-1]["reasoning"], "Thinking round 1\n\nThinking round 2"
        )
        self.assertEqual(events[-1]["text"], "Answer with https://example.org/docs")
        self.assertEqual(events[-1]["sources"][0]["url"], "https://example.org/docs")
        self.assertTrue(any(m["role"] == "tool" for m in requests[1]["messages"]))
        self.assertEqual([event["phase"] for event in events].count("thinking"), 2)
        self.assertIn("loading", [event["phase"] for event in events])

    async def test_off_has_no_tools_and_keeps_thinking_setting(self):
        bodies = []

        async def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=sse(
                    {
                        "id": "r",
                        "model": "local:1",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "Hello"},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                )
                + "data: [DONE]\n\n",
            )

        events = [
            e
            async for e in conversation.stream_reply(
                "local:1",
                False,
                [],
                ["Hello"],
                web="off",
                session_id="a" * 32,
                transport=httpx.MockTransport(handler),
            )
        ]
        self.assertFalse(bodies[0].get("tools"))
        self.assertEqual(bodies[0]["reasoning_effort"], "none")
        self.assertEqual(events[-1]["text"], "Hello")
        instructions = bodies[0]["messages"][0]["content"]
        self.assertIn("content supplied in the conversation", instructions)
        self.assertIn("untrusted reference data", instructions)
