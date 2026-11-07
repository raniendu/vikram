import asyncio
import json
import sys
import tempfile
import unittest

LAUNCHER = r"""
import asyncio, sys
from pathlib import Path
from unittest.mock import patch
from vikram.desktop import __main__ as sidecar
from vikram.desktop.runtime import Runtime

class ControlledRuntime(Runtime):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.models = [{"name": "local:1", "available": True}]
        async def metadata(*args, **kwargs):
            self.event("test_wait", operation="metadata")
            await asyncio.Event().wait()
        async def compact(*args, **kwargs):
            self.event("test_wait", operation="compact")
            await asyncio.Event().wait()
        self.catalog.refresh = metadata
        self.workbench.compact = compact

with patch.object(sidecar, "Runtime", ControlledRuntime):
    asyncio.run(sidecar.serve(Path(sys.argv[1]), browser=False))
"""


class SidecarDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            LAUNCHER,
            self.temporary.name,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.responses = []
        await self.send("create", "create_chat")
        self.chat = (await self.response("create"))["result"]
        await self.send(
            "model",
            "update_chat",
            {"chat_id": self.chat["id"], "fields": {"model": "local:1"}},
        )
        await self.response("model")
        await self.send(
            "draft",
            "draft",
            {"chat_id": self.chat["id"], "text": "Preserve my draft", "revision": 1},
        )
        await self.response("draft")

    async def asyncTearDown(self):
        if self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.temporary.cleanup()

    async def send(self, identifier, method, params=None):
        await self.raw(
            {"v": 1, "id": identifier, "method": method, "params": params or {}}
        )

    async def raw(self, value):
        self.process.stdin.write((json.dumps(value) + "\n").encode())
        await self.process.stdin.drain()

    async def response(self, identifier=None, *, event=None):
        for value in self.responses:
            if (identifier is not None and value.get("id") == identifier) or (
                event is not None and value.get("event") == event
            ):
                self.responses.remove(value)
                return value
        while True:
            line = await asyncio.wait_for(self.process.stdout.readline(), 2)
            self.assertTrue(line, "Sidecar closed before responding")
            value = json.loads(line)
            if (identifier is not None and value.get("id") == identifier) or (
                event is not None and value.get("event") == event
            ):
                return value
            self.responses.append(value)

    async def check_interrupt(self, method, params):
        await self.send("long", method, params)
        await self.send(
            "overlap",
            "workbench",
            {"action": "compact", "chat_id": self.chat["id"]},
        )
        await self.response(event="test_wait")
        self.assertIn("error", await self.response("overlap"))
        await self.send(
            "queue",
            "workbench",
            {
                "action": "queue",
                "chat_id": self.chat["id"],
                "value": "Queue while startup waits",
            },
        )
        queued = (await self.response("queue"))["result"]
        self.assertEqual(queued[0]["text"], "Queue while startup waits")
        await self.send(
            "respond",
            "respond",
            {
                "chat_id": self.chat["id"],
                "attempt_id": "a" * 32,
                "interaction_id": "missing",
                "kind": "question",
                "cancelled": True,
            },
        )
        self.assertIn("error", await self.response("respond"))
        await self.send("stop", "stop")
        self.assertIn("result", await self.response("stop"))
        self.assertIn("cancelled", (await self.response("long"))["error"].lower())
        self.assertIsNone(self.process.returncode)
        await self.send("snapshot", "chat", {"chat_id": self.chat["id"]})
        saved = (await self.response("snapshot"))["result"]
        self.assertEqual(saved["draft"], "Preserve my draft")
        self.assertEqual(saved["messages"], [])

    async def test_send_startup_does_not_block_stop_respond_or_queue(self):
        await self.check_interrupt(
            "send", {"chat_id": self.chat["id"], "request_id": "generation"}
        )

    async def test_workbench_wait_does_not_block_stop_respond_or_queue(self):
        await self.check_interrupt(
            "workbench", {"action": "compact", "chat_id": self.chat["id"]}
        )

    async def test_task_limit_validates_payload_before_reporting_busy_and_keeps_quick_order(
        self,
    ):
        for number in range(8):
            await self.send(f"refresh-{number}", "refresh")
            await self.response(event="test_wait")
        await self.raw({"v": 1, "id": "malformed", "method": "refresh", "params": []})
        malformed = await self.response("malformed")
        self.assertIn("Invalid desktop command", malformed["error"])
        await self.send("busy", "refresh")
        self.assertIn("finish", (await self.response("busy"))["error"])
        await self.send(
            "draft-next",
            "draft",
            {"chat_id": self.chat["id"], "text": "Ordered draft", "revision": 2},
        )
        await self.send("read-next", "chat", {"chat_id": self.chat["id"]})
        self.assertIn("result", await self.response("draft-next"))
        self.assertEqual(
            (await self.response("read-next"))["result"]["draft"], "Ordered draft"
        )
        self.process.stdin.close()
        await asyncio.wait_for(self.process.wait(), 3)
        self.assertEqual(self.process.returncode, 0)


if __name__ == "__main__":
    unittest.main()
