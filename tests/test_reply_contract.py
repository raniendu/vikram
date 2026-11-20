"""Regressions for ephemeral files, exhausted budgets, and false completion."""

import json
import tempfile
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import patch

import httpx
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.usage import UsageLimits
from test_desktop_runtime import ENTRY, SHOW
from test_toolkit import FakeSandbox, Model, fake_vm, system_prompt, tool_call

from vikram.coding import CodingSession
from vikram.context import activity_note
from vikram.conversation import stream_reply
from vikram.desktop.runtime import Runtime
from vikram.desktop.store import Store
from vikram.errors import ReplyError
from vikram.integrations import close
from vikram.models import parse_model
from vikram.settings import Settings
from vikram.toolkit import LIMITS

RAW_CALL = (
    "<tool_call>\n<function=run_code>\n<parameter=code>\n"
    "print('not executed')\n</parameter>\n</function>\n</tool_call>"
)


def setUpModule():
    unittest.enterModuleContext(fake_vm())


class ReplyContractTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, model, **options):
        return [
            event
            async for event in stream_reply(
                "local:1",
                False,
                [],
                "Build a file",
                web="auto",
                transport=httpx.MockTransport(model),
                **options,
            )
        ]

    async def test_no_workspace_explains_file_limit_and_trust_workflow(self):
        model = Model("Choose a workspace first.")
        await self.collect(model)
        prompt = system_prompt(model.bodies[0])
        self.assertIn("deleted", prompt)
        self.assertIn("not a Mac path", prompt)
        self.assertIn("Workbench", prompt)
        self.assertIn("/workspace", prompt)
        self.assertIn("/trust", prompt)
        self.assertNotIn("write", model.offered(0))

    async def test_trusted_workspace_files_survive_the_next_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            settings = Settings(root=root / "settings", workspace=workspace)
            settings.trust()
            coding = CodingSession(workspace, settings=settings)
            try:
                first = Model(
                    tool_call("write", {"path": "result.txt", "content": "kept"}),
                    "Saved result.txt.",
                )
                await self.collect(first, coding=coding)
                prompt = system_prompt(first.bodies[0])
                self.assertIn(str(workspace), prompt)
                self.assertIn("persistent", prompt)
                self.assertIn("not run_code", prompt)
                second = Model(
                    tool_call("read", {"path": "result.txt"}), "The file is kept."
                )
                events = await self.collect(second, coding=coding)
                self.assertEqual((workspace / "result.txt").read_text(), "kept")
                record = events[-1]["activity"][0]
                self.assertEqual(record["status"], "complete")
                self.assertIn("kept", record["output"])
            finally:
                await close(coding)

    async def test_custom_request_budget_reserves_a_tool_free_final_answer(self):
        def step(body):
            return (
                tool_call("run_code", {"code": "1"}, f"c{len(model.bodies)}")
                if body.get("tools")
                else "Only the verified result is available; the build is unfinished."
            )

        model = Model(step, step, step)
        events = await self.collect(model, usage_limits=UsageLimits(request_limit=3))
        self.assertEqual(len(model.bodies), 3)
        self.assertFalse(model.bodies[-1].get("tools"))
        self.assertIn("final", system_prompt(model.bodies[-1]).lower())
        self.assertIn("unfinished", events[-1]["text"])

    async def test_tool_call_text_is_repaired_without_executing_the_text(self):
        sandbox = FakeSandbox()
        model = Model(RAW_CALL, "No code was executed.")
        events = await self.collect(model, sandbox=lambda: sandbox)
        self.assertEqual(len(model.bodies), 2)
        self.assertEqual(sandbox.codes, [])
        self.assertEqual(events[-1]["text"], "No code was executed.")
        self.assertTrue(sandbox.closed)

    async def test_spent_sandbox_recovers_with_a_plain_answer_and_no_replay(self):
        sandbox = FakeSandbox()

        def finish(body):
            if body.get("tools") or RAW_CALL in [
                m.get("content") for m in body["messages"]
            ]:
                return RAW_CALL
            return "The sandbox returned ok. The Mac file is unfinished."

        model = Model(tool_call("run_code", {"code": "print(1)"}), RAW_CALL, finish)
        with patch.dict(LIMITS, {"run_code": 1}):
            events = await self.collect(model, sandbox=lambda: sandbox)
        self.assertFalse(model.offered(2))
        final = model.bodies[2]
        self.assertNotIn(RAW_CALL, [m.get("content") for m in final["messages"]])
        self.assertNotIn("call it before", system_prompt(final))
        self.assertIn("Successful sandbox runs: 1 of 1", system_prompt(final))
        self.assertFalse(
            any(m.get("tool_calls") or m["role"] == "tool" for m in final["messages"])
        )
        self.assertIn("ok", str([m["content"] for m in final["messages"][1:]]))
        self.assertEqual(
            events[-1]["text"], "The sandbox returned ok. The Mac file is unfinished."
        )
        self.assertEqual(sandbox.codes, ["print(1)"])

    async def test_handoff_at_limit_preserves_history_and_current_steering(self):
        model = Model(tool_call("run_code", {"code": "large_generated_source"}), "Done")
        history = [
            ModelRequest(parts=[UserPromptPart(content="Previous request")]),
            ModelResponse(parts=[TextPart("Previous answer")]),
        ]
        steering = iter([[], ["Use the short format."]])
        with patch.dict(LIMITS, {"run_code": 1}):
            async for _ in stream_reply(
                "local:1",
                None,
                history,
                "Inspect the sandbox",
                web="auto",
                sandbox=FakeSandbox,
                transport=httpx.MockTransport(model),
                request_boundary=lambda: next(steering),
                usage_limits=UsageLimits(request_limit=2),
            ):
                pass
        final = model.bodies[-1]["messages"]
        self.assertEqual(
            [m["content"] for m in final if m["role"] == "assistant"],
            ["Previous answer"],
        )
        self.assertEqual(final[-1]["content"], "Use the short format.")
        self.assertIn("Inspect the sandbox", str(final))
        self.assertIn("untrusted reference data", str(final))
        self.assertNotIn("large_generated_source", str(final))
        self.assertFalse(any(m["role"] == "tool" for m in final))

    async def test_tool_call_text_at_the_budget_boundary_fails_and_is_cleared(self):
        sandbox = FakeSandbox()
        model = Model(RAW_CALL)
        events = []
        with self.assertRaisesRegex(ReplyError, "not executed"):
            async for event in stream_reply(
                "local:1",
                False,
                [],
                "Open it",
                web="auto",
                transport=httpx.MockTransport(model),
                sandbox=lambda: sandbox,
                usage_limits=UsageLimits(request_limit=1),
            ):
                events.append(event)
        self.assertEqual(len(model.bodies), 1)
        self.assertEqual(events[-1]["text"], "")
        self.assertEqual(sandbox.codes, [])
        self.assertTrue(sandbox.closed)

    async def test_repeated_malformed_answers_have_bounded_corrections(self):
        model = Model(*([RAW_CALL] * 10))
        with self.assertRaisesRegex(ReplyError, "incomplete"):
            await self.collect(model)
        self.assertLessEqual(len(model.bodies), 3)

    async def test_structured_tool_on_final_request_is_not_executed(self):
        sandbox = FakeSandbox()
        model = Model(tool_call("run_code", {"code": "print('unexpected')"}))
        with self.assertRaisesRegex(ReplyError, "not executed"):
            await self.collect(
                model,
                sandbox=lambda: sandbox,
                usage_limits=UsageLimits(request_limit=1),
            )
        self.assertEqual(sandbox.codes, [])
        self.assertEqual(len(model.bodies), 1)

    async def test_explicit_unlimited_budget_is_not_replaced_by_ten_requests(self):
        model = Model(
            *(tool_call("run_code", {"code": "1"}, f"c{i}") for i in range(11)),
            "Done",
        )
        with patch.dict(LIMITS, {"run_code": 20}):
            events = await self.collect(
                model, usage_limits=UsageLimits(request_limit=None)
            )
        self.assertEqual(len(model.bodies), 12)
        self.assertEqual(events[-1]["text"], "Done")

    async def test_tool_free_models_also_reject_unexecuted_markup(self):
        model = Model(RAW_CALL)
        with self.assertRaisesRegex(ReplyError, "not executed"):
            _ = [
                event
                async for event in stream_reply(
                    "local:1",
                    False,
                    [],
                    "Open it",
                    web="off",
                    transport=httpx.MockTransport(model),
                    usage_limits=UsageLimits(request_limit=1),
                )
            ]
        self.assertFalse(model.bodies[0].get("tools"))

    async def test_quoted_tool_documentation_is_not_rejected(self):
        answer = "Example syntax, not an action:\n```xml\n" + RAW_CALL + "\n```"
        model = Model(answer)
        events = await self.collect(model)
        self.assertEqual(events[-1]["text"], answer)
        self.assertEqual(len(model.bodies), 1)

    async def test_truncated_response_fails_without_automatic_replay(self):
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            payload = {
                "id": "r",
                "model": "local:1",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "An unfinished answer"},
                        "finish_reason": "length",
                    }
                ],
            }
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n",
            )

        with self.assertRaisesRegex(ReplyError, "output limit"):
            await self.collect(handler)
        self.assertEqual(len(requests), 1)

    async def test_desktop_saves_malformed_final_answer_as_failed_not_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            model = Model(RAW_CALL)
            runtime = Runtime(
                store,
                lambda event: None,
                streamer=partial(
                    stream_reply,
                    transport=httpx.MockTransport(model),
                    usage_limits=UsageLimits(request_limit=1),
                ),
            )

            async def discover(*, include_cloud=False, force=False):
                return [
                    parse_model(
                        ENTRY,
                        {**SHOW, "capabilities": SHOW["capabilities"] + ["tools"]},
                    )
                ]

            runtime.catalog.refresh = discover
            try:
                cid = store.create_chat("local:1")["id"]
                store.save_draft(cid, "Open it", 1)
                await runtime.send(cid, "malformed")
                assert runtime.task is not None
                await runtime.task
                attempt = store.chat(cid)["messages"][-1]["attempts"][-1]
                self.assertEqual(attempt["status"], "failed")
                self.assertIn("not executed", attempt["error"])
                self.assertNotIn("<tool_call>", attempt["content"])
            finally:
                await runtime.close()
                store.close()

    def test_followup_marks_old_sandbox_files_deleted_and_keeps_failures(self):
        note = activity_note(
            [
                {
                    "kind": "code",
                    "status": "complete",
                    "code": "open('/workspace/result.txt', 'w').write('kept')",
                    "output": "4",
                },
                {
                    "kind": "code",
                    "status": "failed",
                    "code": "open('/workspace/missing.txt')",
                    "output": "",
                    "error": "FileNotFoundError",
                },
            ]
        )
        assert note is not None
        self.assertIn("deleted", note)
        self.assertIn("FileNotFoundError", note)
        self.assertIn("failed", note)
