import json
import tempfile
import unittest
from pathlib import Path

import httpx
from test_conversation import offered, sse
from test_toolkit import fake_vm

from vikram.coding import CodingSession
from vikram.conversation import stream_reply
from vikram.settings import Settings


def setUpModule():
    unittest.enterModuleContext(fake_vm())


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_coding_dispatch_and_steering_share_model_request_boundaries(self):
        bodies = []
        queued = []

        def boundary():
            result = queued[:]
            queued.clear()
            return result

        async def server(request):
            body = json.loads(request.content)
            bodies.append(body)
            if len(bodies) == 1:
                queued.append("Also explain the file")
                delta = {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "read-1",
                            "type": "function",
                            "function": {
                                "name": "read",
                                "arguments": json.dumps({"path": "hello.txt"}),
                            },
                        }
                    ]
                }
                reason = "tool_calls"
            else:
                delta = {"content": "The file says hello."}
                reason = "stop"
            text = sse(
                {
                    "id": "r",
                    "model": "local:1",
                    "choices": [{"index": 0, "delta": delta}],
                }
            )
            text += (
                sse(
                    {
                        "id": "r",
                        "model": "local:1",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
                    }
                )
                + "data: [DONE]\n\n"
            )
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, text=text
            )

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "hello.txt").write_text("hello")
            settings = Settings(root=root / "state", workspace=root)
            settings.trust()
            coding = CodingSession(root, settings=settings)
            events = [
                event
                async for event in stream_reply(
                    "local:1",
                    None,
                    [],
                    ["Read hello.txt"],
                    web="auto",
                    coding=coding,
                    request_boundary=boundary,
                    transport=httpx.MockTransport(server),
                )
            ]
            self.assertIn("read", offered(bodies[0]))
            self.assertIn("Also explain the file", json.dumps(bodies[1]["messages"]))
            self.assertIn("hello", json.dumps(bodies[1]["messages"]))
            self.assertEqual(events[-1]["activity"][-1]["kind"], "coding")
            self.assertEqual(events[-1]["activity"][-1]["status"], "complete")
            await coding.close()


class DynamicExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(root=self.root / "state", workspace=self.root)
        self.settings.trust()
        self.coding = CodingSession(self.root, settings=self.settings)
        self.addAsyncCleanup(self.coding.close)

    async def run_steps(self, steps, **kwargs):
        bodies = []

        async def server(request):
            body = json.loads(request.content)
            bodies.append(body)
            step = steps[len(bodies) - 1]
            if isinstance(step, tuple):
                name, arguments = step
                delta = {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": str(len(bodies)),
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ]
                }
                reason = "tool_calls"
            else:
                delta, reason = {"content": step}, "stop"
            content = sse(
                {
                    "id": "r",
                    "model": "local:1",
                    "choices": [{"index": 0, "delta": delta}],
                }
            )
            content += (
                sse(
                    {
                        "id": "r",
                        "model": "local:1",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
                    }
                )
                + "data: [DONE]\n\n"
            )
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, text=content
            )

        events = [
            event
            async for event in stream_reply(
                "local:1",
                None,
                [],
                ["Work"],
                web="auto",
                coding=self.coding,
                transport=httpx.MockTransport(server),
                **kwargs,
            )
        ]
        return bodies, events

    async def test_search_exposes_matching_mcp_schema_next_request(self):
        from unittest.mock import patch

        from vikram.mcp_client import MCPTool

        definition = MCPTool(
            name="mcp__fake__lookup",
            server="fake",
            original_name="lookup",
            description="Look up a value",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            read_only=True,
            exposed=False,
        )

        class Manager:
            def begin_reply(self):
                pass

            async def list_tools(self, exposed_only=False):
                return [] if exposed_only else [definition]

            async def search_tools(self, query, limit):
                return [definition]

            async def call_tool(self, name, args, **kwargs):
                return {"ok": True, "output": "found"}

        from vikram.extensions import ExtensionManager

        self.settings.set("mcp_servers", {"fake": {"command": "fake"}})
        self.coding.mcp = Manager()
        self.coding.extensions = ExtensionManager(self.settings)
        with patch("vikram.integrations.start"):
            bodies, events = await self.run_steps(
                [
                    ("mcp_search_tools", {"query": "lookup"}),
                    ("mcp__fake__lookup", {}),
                    "Done",
                ]
            )
        self.assertNotIn(definition.name, offered(bodies[0]))
        self.assertIn(definition.name, offered(bodies[1]))
        self.assertIn("found", json.dumps(bodies[2]["messages"]))
        self.assertEqual(events[-1]["activity"][0]["kind"], "mcp")

    async def test_composition_uses_same_dispatch_and_refuses_host_mutation_in_plan(
        self,
    ):
        from unittest.mock import patch

        (self.root / "file.txt").write_text("original")
        observed = []

        async def vm(code, *, dispatcher, tools, state):
            names = {tool["name"] for tool in tools}
            self.assertIn("read", names)
            self.assertIn("web_search", names)
            self.assertNotIn("run_code", names)
            self.assertNotIn("compose_tools", names)
            result = await dispatcher("read", {"path": "file.txt"}, "nested-1")
            observed.append(result)
            return {"ok": True, "output": result["output"]}

        with patch("vikram.execution.compose_tools", side_effect=vm) as run:
            _, events = await self.run_steps(
                [
                    (
                        "compose_tools",
                        {"code": "await call_tool('read', path='file.txt')"},
                    ),
                    "Done",
                ]
            )
            self.assertEqual(run.await_count, 1)
            self.assertIn("original", observed[0]["output"])
            self.assertEqual(
                [item["kind"] for item in events[-1]["activity"]],
                ["composition", "coding"],
            )
            await self.run_steps(
                [("compose_tools", {"code": "print('no')"}), "Planning"], mode="plan"
            )
            self.assertEqual(run.await_count, 1)


class InstructionScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_nested_rules_reach_model_before_mutation(self):
        from vikram.execution import tool_call
        from vikram.toolkit import Reply

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            nested = root / "src"
            nested.mkdir()
            (nested / "AGENTS.md").write_text("All files here must say approved.")
            settings = Settings(root=root / "state", workspace=root)
            settings.trust()
            coding = CodingSession(root, settings=settings)
            self.addAsyncCleanup(coding.close)
            reply = Reply(coding=coding)
            args = {"path": "src/new.txt", "content": "approved"}
            first = await tool_call(reply, "write", args, "first", request_index=1)
            self.assertFalse(first["ok"])
            self.assertFalse((nested / "new.txt").exists())
            self.assertIn("All files here must say approved", reply.instructions)
            second = await tool_call(reply, "write", args, "second", request_index=2)
            self.assertTrue(second["ok"])
            self.assertEqual((nested / "new.txt").read_text(), "approved")


class ApprovalDetailTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_approval_shows_parameters_and_redacts_secrets(self):
        from vikram.execution import ObservedApproval
        from vikram.interaction import ActionRequest
        from vikram.toolkit import Reply

        class Person:
            async def approve(self, request):
                self.request = request
                return False

        person = Person()
        reply = Reply(interaction=person)
        record = {
            "arguments": {"recipient": "review@example.test", "api_key": "private-key"}
        }
        approved = await ObservedApproval(reply, record).approve(
            ActionRequest(
                id="call", summary="Send", reason="Remote effect", url="", kind="mcp"
            )
        )
        self.assertFalse(approved)
        self.assertIn("review@example.test", person.request.details)
        self.assertNotIn("private-key", person.request.details)
        self.assertEqual(record["approval"]["details"], person.request.details)
