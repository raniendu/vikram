import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from unittest.mock import patch

import httpx
from test_conversation import offered, sse
from test_desktop_runtime import ENTRY, SHOW

from vikram import cli
from vikram.cli import ChatSession
from vikram.conversation import stream_reply
from vikram.desktop.runtime import Runtime
from vikram.desktop.store import Store
from vikram.models import parse_model
from vikram.web import WebSession


class ParityTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_failures_are_actionable_private_and_shared(self):
        for kind, expected in (
            ("timeout", "timed out"),
            ("connection", "connect"),
            ("busy", "busy"),
            ("missing", "not available"),
            ("internal", "restart"),
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:

                async def handler(request):
                    if kind == "timeout":
                        raise httpx.ReadTimeout("private prompt", request=request)
                    if kind == "connection":
                        raise httpx.ConnectError("private prompt", request=request)
                    if kind == "internal":
                        raise ImportError("private prompt")
                    return httpx.Response(
                        503 if kind == "busy" else 404,
                        json={"error": "private prompt"},
                    )

                streamer = partial(stream_reply, transport=httpx.MockTransport(handler))

                async def discover():
                    return [parse_model(ENTRY, SHOW)]

                session = ChatSession("local:1", streamer=streamer)
                session.catalog.refresh = discover
                with self.assertRaises(Exception) as captured:
                    _ = [e async for e in session.reply("Hello")]

                store = Store(Path(root))
                try:
                    cid = store.create_chat("local:1")["id"]
                    store.save_draft(cid, "Hello", 1)
                    runtime = Runtime(store, lambda e: None, streamer=streamer)
                    runtime.catalog.refresh = discover
                    await runtime.send(cid, "failure")
                    await runtime.task
                    attempt = store.chat(cid)["messages"][-1]["attempts"][-1]
                    self.assertEqual(attempt["status"], "failed")
                    self.assertIn(expected, attempt["error"].lower())
                    self.assertNotIn("private prompt", attempt["error"])
                    output = io.StringIO()
                    with (
                        contextlib.redirect_stderr(output),
                        patch("vikram.cli._chat", side_effect=captured.exception),
                    ):
                        self.assertEqual(cli.main(["--model", "local:1", "Hello"]), 1)
                    self.assertEqual(output.getvalue().strip(), attempt["error"])
                finally:
                    store.close()

    async def test_capability_change_resets_thinking_equally_and_disables_web(self):
        traces = [[], []]

        def streamer_for(index):
            async def stream(model, thinking, history, prompt, **options):
                traces[index].append((thinking, options["web"]))
                yield {"text": "Answer", "phase": "answering"}

            return stream

        metadata = parse_model(ENTRY, SHOW)

        async def discover():
            return [metadata]

        with tempfile.TemporaryDirectory() as root:
            cli = ChatSession("local:1", thinking="off", streamer=streamer_for(0))
            cli.catalog.refresh = discover
            store = Store(Path(root))
            cid = store.create_chat("local:1")["id"]
            store.update_chat(cid, thinking={"local:1": False})
            runtime = Runtime(store, lambda event: None, streamer=streamer_for(1))
            runtime.catalog.refresh = discover
            for revision in range(1, 4):
                if revision == 2:
                    metadata = {**metadata, "thinking": []}
                if revision == 3:
                    metadata = parse_model(ENTRY, SHOW)
                events = [e async for e in cli.reply("Hello")]
                store.save_draft(cid, "Hello", revision * 10)
                await runtime.send(cid, str(revision))
                await runtime.task
            self.assertEqual(traces[0], traces[1])
            # Off while advertised; nothing to request without controls; then the
            # reset choice is the default again, which is thinking on.
            self.assertEqual(traces[0], [(False, "off"), (None, "off"), (True, "off")])
            self.assertEqual(cli.thinking, "on")
            self.assertEqual(store.chat(cid)["thinking"], {"local:1": None})
            store.close()

    async def test_cli_and_desktop_send_identical_tool_turns_and_followup_context(self):
        await self.assert_tool_parity(failure=False)

    async def test_cli_and_desktop_send_identical_failed_tool_turns(self):
        await self.assert_tool_parity(failure=True)

    async def test_legacy_web_off_chat_gets_the_same_tools_as_default_cli(self):
        await self.assert_tool_parity(failure=False, legacy_off=True)

    async def assert_tool_parity(self, *, failure, legacy_off=False):
        traces = [[], []]

        async def discover():
            return [
                parse_model(
                    ENTRY, {**SHOW, "capabilities": SHOW["capabilities"] + ["tools"]}
                )
            ]

        def streamer_for(index):
            async def handler(request):
                body = json.loads(request.content)
                traces[index].append(body)
                tool = len(traces[index]) == 1
                delta = (
                    {
                        "content": "Looking it up",
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
                    if tool
                    else {"content": "Evidence-based answer"}
                )
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
                                    "finish_reason": "tool_calls" if tool else "stop",
                                }
                            ],
                        }
                    )
                    + "data: [DONE]\n\n",
                )

            return partial(stream_reply, transport=httpx.MockTransport(handler))

        async def remote(*args):
            if failure:
                raise RuntimeError("Private response body must not reach context")
            return {
                "results": [
                    {
                        "url": "https://example.org/docs",
                        "title": "Docs",
                        "excerpts": ["Evidence"],
                    }
                ]
            }

        with (
            patch("vikram.web.datetime") as clock,
            patch("vikram.agent.datetime") as system_clock,
            tempfile.TemporaryDirectory() as root,
            patch(
                "vikram.conversation.WebSession",
                side_effect=lambda sid, **kwargs: WebSession(
                    sid, remote=remote, **kwargs
                ),
            ),
        ):
            clock.now.return_value = datetime(2026, 10, 14, tzinfo=timezone.utc)
            system_clock.now.return_value.astimezone.return_value = (
                clock.now.return_value
            )
            cli = ChatSession("local:1", thinking="off", streamer=streamer_for(0))
            cli.catalog.refresh = discover
            store = Store(Path(root))
            chat = store.create_chat("local:1")
            cid = chat["id"]
            if legacy_off:
                with store.db:
                    store.db.execute("UPDATE chats SET web='off' WHERE id=?", (cid,))
            store.update_chat(cid, thinking={"local:1": False})
            runtime = Runtime(store, lambda event: None, streamer=streamer_for(1))
            runtime.catalog.refresh = discover
            for revision, prompt in enumerate(["Find docs", "What did they say?"], 1):
                cli_events = [e async for e in cli.reply(prompt)]
                store.save_draft(cid, prompt, revision * 10)
                await runtime.send(cid, "r" + str(revision))
                await runtime.task
            self.assertEqual(traces[0], traces[1])
            self.assertEqual(len(traces[0]), 3)
            if failure:
                self.assertIn("unavailable", json.dumps(traces[0][1]["messages"]))
                self.assertNotIn("Private response", json.dumps(traces))
                self.assertNotIn("web_search", offered(traces[0][1]))
                self.assertIn("run_code", offered(traces[0][1]))
            else:
                self.assertIn("Evidence", json.dumps(traces[0][-1]["messages"]))
            self.assertEqual(
                store.chat(cid)["messages"][-1]["attempts"][-1]["status"], "complete"
            )
            store.close()


class InteractionParityTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_and_desktop_send_identical_question_and_plan_turns(self):
        from test_toolkit import Model, tool_call

        from vikram.interaction import ScriptedInteraction

        def model():
            return Model(
                tool_call(
                    "ask_user_question",
                    {
                        "questions": [
                            {
                                "header": "Scale",
                                "question": "Which scale?",
                                "options": [
                                    {"label": "Celsius"},
                                    {"label": "Fahrenheit"},
                                ],
                            }
                        ]
                    },
                    "q1",
                ),
                tool_call("submit_plan", {"plan": "1. Convert."}, "p1"),
                tool_call("run_code", {"code": "(25 - 32) * 5 / 9"}, "c1"),
                "-3.9 °C",
            )

        async def discover():
            return [
                parse_model(
                    ENTRY, {**SHOW, "capabilities": SHOW["capabilities"] + ["tools"]}
                )
            ]

        models = [model(), model()]
        with (
            patch("vikram.agent.datetime") as clock,
            tempfile.TemporaryDirectory() as root,
        ):
            clock.now.return_value.astimezone.return_value = datetime(
                2026, 10, 20, tzinfo=timezone.utc
            )
            cli = ChatSession(
                "local:1",
                thinking="off",
                mode="plan",
                streamer=partial(
                    stream_reply, transport=httpx.MockTransport(models[0])
                ),
            )
            cli.catalog.refresh = discover
            _ = [
                e
                async for e in cli.reply(
                    "Convert 25 degrees",
                    interaction=ScriptedInteraction(pattern="fahrenheit"),
                )
            ]
            store = Store(Path(root))
            cid = store.create_chat("local:1")["id"]
            store.update_chat(cid, thinking={"local:1": False}, mode="plan")

            def answer(event):
                if event["event"] != "attempt" or event["status"] != "running":
                    return
                for record in event.get("activity", []):
                    if record["status"] != "waiting":
                        continue
                    params = (
                        {"answers": [{"header": "Scale", "selected": ["Fahrenheit"]}]}
                        if record["kind"] == "question"
                        else {"decision": "approve"}
                    )
                    asyncio.get_running_loop().call_soon(
                        runtime.respond,
                        {
                            "chat_id": cid,
                            "attempt_id": event["attempt_id"],
                            "interaction_id": record["id"],
                            "kind": record["kind"],
                            **params,
                        },
                    )

            runtime = Runtime(
                store,
                answer,
                streamer=partial(
                    stream_reply, transport=httpx.MockTransport(models[1])
                ),
            )
            runtime.catalog.refresh = discover
            store.save_draft(cid, "Convert 25 degrees", 10)
            await runtime.send(cid, "r1")
            await runtime.task
            attempt = store.chat(cid)["messages"][-1]["attempts"][-1]
            self.assertEqual(attempt["status"], "complete", attempt["error"])
            self.assertEqual(models[0].bodies, models[1].bodies)
            self.assertEqual(len(models[0].bodies), 4)
            self.assertEqual(cli.mode, "normal")
            self.assertEqual(store.chat(cid)["mode"], "normal")
            self.assertEqual(
                [(r["kind"], r["status"]) for r in cli.turns[-1]["activity"]],
                [(r["kind"], r["status"]) for r in attempt["activity"]],
            )
            store.close()
