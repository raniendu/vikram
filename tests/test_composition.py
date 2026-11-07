import asyncio
import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from vikram.composition import CompositionState, approval_wait, compose_tools
from vikram.sandbox import CodeRun, SandboxUnavailable, VmSandbox

TOOLS = [
    {
        "name": "read",
        "read_only": True,
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write",
        "read_only": False,
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {"name": "run_code", "input_schema": {"type": "object"}},
    {"name": "compose_tools", "input_schema": {"type": "object"}},
]


class FakeCompositionVM:
    """Models the guest/control protocol without executing model code on the host."""

    def __init__(self, requests=(), *, wait=False):
        self.frames = list(requests)
        self.expected = len(self.frames)
        self.wait = wait
        self.responses = {}
        self.closed = False
        self.prepared = False
        self.release = asyncio.Event()

    async def prepare(self, code, tools, execution_id):
        self.prepared = True
        self.code, self.tools, self.execution_id = code, tools, execution_id

    async def run_composition(self):
        await self.release.wait()
        return CodeRun(0, "guest complete", "")

    async def poll_requests(self):
        frames, self.frames = self.frames[:5], self.frames[5:]
        if not self.expected and not self.wait:
            self.release.set()
        return frames

    async def write_response(self, identifier, result):
        self.responses[identifier] = result
        if len(self.responses) >= self.expected and not self.wait:
            self.release.set()

    async def close(self):
        self.closed = True
        self.release.set()


def request(identifier, name="read", **arguments):
    return {"id": identifier, "name": name, "arguments": arguments}


class CompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_uses_shared_ids_and_excludes_recursive_tools(self):
        vm = FakeCompositionVM([request("one", path="a"), request("two", "run_code")])
        calls = []

        async def dispatch(name, args, call_id):
            calls.append((name, args, call_id))
            return {"ok": True, "output": "read"}

        result = await compose_tools(
            "await call_tool('read', path='a')",
            dispatcher=dispatch,
            tools=TOOLS,
            sandbox=vm,
        )
        self.assertTrue(result["ok"])
        self.assertEqual([tool["name"] for tool in vm.tools], ["read", "write"])
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][2].endswith(":one"))
        self.assertFalse(vm.responses["two"]["ok"])
        self.assertTrue(vm.closed)

    async def test_deduplication_persists_across_composition_runs(self):
        state, calls = CompositionState(), []

        async def dispatch(name, args, call_id):
            calls.append(name)
            return {"ok": True, "output": "written"}

        for _ in range(2):
            vm = FakeCompositionVM([request("same", "write", path="a")])
            result = await compose_tools(
                "pass", dispatcher=dispatch, tools=TOOLS, sandbox=vm, state=state
            )
            self.assertTrue(result["ok"])
        self.assertEqual(calls, ["write"])
        vm = FakeCompositionVM([request("same", "write", path="different")])
        await compose_tools(
            "pass", dispatcher=dispatch, tools=TOOLS, sandbox=vm, state=state
        )
        self.assertFalse(vm.responses["same"]["ok"])
        self.assertEqual(calls, ["write"])

    async def test_read_concurrency_and_serialized_mutations(self):
        active, maximum = {"read": 0, "write": 0}, {"read": 0, "write": 0}
        calls = []

        async def dispatch(name, args, call_id):
            active[name] += 1
            maximum[name] = max(maximum[name], active[name])
            await asyncio.sleep(0.01)
            active[name] -= 1
            calls.append(call_id)
            return {"ok": True}

        frames = [request(f"r{i}", path="a") for i in range(8)] + [
            request(f"w{i}", "write", path="a") for i in range(4)
        ]
        vm = FakeCompositionVM(frames)
        await compose_tools("pass", dispatcher=dispatch, tools=TOOLS, sandbox=vm)
        self.assertEqual(len(calls), 12)
        self.assertEqual(maximum["read"], 4)
        self.assertEqual(maximum["write"], 1)

    async def test_cancellation_destroys_vm_and_preserves_call_tombstone(self):
        state = CompositionState()
        started = asyncio.Event()
        calls = []

        async def dispatch(name, args, call_id):
            calls.append(call_id)
            started.set()
            await asyncio.Event().wait()

        vm = FakeCompositionVM([request("cancel", "write", path="a")], wait=True)
        task = asyncio.create_task(
            compose_tools(
                "pass", dispatcher=dispatch, tools=TOOLS, sandbox=vm, state=state
            )
        )
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(vm.closed)
        retry = FakeCompositionVM([request("cancel", "write", path="a")])
        await compose_tools(
            "pass", dispatcher=dispatch, tools=TOOLS, sandbox=retry, state=state
        )
        self.assertFalse(retry.responses["cancel"]["ok"])
        self.assertEqual(len(calls), 1)

    async def test_active_deadline_excludes_approval_wait_but_total_deadline_does_not(
        self,
    ):
        async def dispatch(name, args, call_id):
            with approval_wait():
                await asyncio.sleep(0.08)
            return {"ok": False, "error": "Declined"}

        state = CompositionState(active_timeout=0.05, total_timeout=1)
        vm = FakeCompositionVM([request("approval", "write", path="a")])
        result = await compose_tools(
            "pass", dispatcher=dispatch, tools=TOOLS, sandbox=vm, state=state
        )
        self.assertTrue(result["ok"])
        self.assertFalse(vm.responses["approval"]["ok"])
        vm = FakeCompositionVM([request("total", "write", path="a")])
        result = await compose_tools(
            "pass",
            dispatcher=dispatch,
            tools=TOOLS,
            sandbox=vm,
            state=CompositionState(active_timeout=1, total_timeout=0.03),
        )
        self.assertFalse(result["ok"])
        self.assertIn("overall", result["error"])
        self.assertTrue(vm.closed)

    async def test_schema_frame_and_aggregate_call_limits_fail_closed(self):
        calls = []

        async def dispatch(name, args, call_id):
            calls.append(name)
            return {"ok": True}

        vm = FakeCompositionVM(
            [
                request("invalid", path=3),
                request("valid", path="a"),
                request("limit", path="b"),
            ]
        )
        await compose_tools(
            "pass",
            dispatcher=dispatch,
            tools=TOOLS,
            sandbox=vm,
            state=CompositionState(max_calls=2),
        )
        self.assertFalse(vm.responses["invalid"]["ok"])
        self.assertTrue(vm.responses["valid"]["ok"])
        self.assertFalse(vm.responses["limit"]["ok"])
        self.assertEqual(calls, ["read"])
        vm = FakeCompositionVM([request("large", path="x" * (1024 * 1024))])
        result = await compose_tools(
            "pass", dispatcher=dispatch, tools=TOOLS, sandbox=vm
        )
        self.assertFalse(result["ok"])
        self.assertTrue(vm.closed)

    async def test_active_deadline_stops_guest_and_boot_wait(self):
        async def dispatch(name, args, call_id):
            self.fail("No calls expected")

        vm = FakeCompositionVM(wait=True)
        result = await compose_tools(
            "while True: pass",
            dispatcher=dispatch,
            tools=[],
            sandbox=vm,
            state=CompositionState(active_timeout=0.025),
        )
        self.assertFalse(result["ok"])
        self.assertIn("active", result["error"])
        self.assertTrue(vm.closed)
        vm = FakeCompositionVM(wait=True)

        async def slow_prepare(*args):
            await asyncio.sleep(30)

        vm.prepare = slow_prepare
        result = await compose_tools(
            "pass",
            dispatcher=dispatch,
            tools=[],
            sandbox=vm,
            state=CompositionState(active_timeout=0.025),
        )
        self.assertFalse(result["ok"])
        self.assertIn("active", result["error"])
        self.assertTrue(vm.closed)

    async def test_vm_cleanup_captures_reference_before_backend_destroy(self):
        vm = VmSandbox()
        ref = SimpleNamespace(id="pydantic-ai-test")
        backend = SimpleNamespace(ref=ref)
        vm._backend = backend

        class Smol:
            async def destroy(self, actual):
                self.assert_reference = actual
                backend.ref = None

        with tempfile.TemporaryDirectory() as temporary:
            markers = Path(temporary)
            marker = markers / "pydantic-ai-test.json"
            marker.write_text("{}")
            with (
                patch("vikram.sandbox._markers", return_value=markers),
                patch("pydantic_ai_harness.smol_sandbox.SmolSandbox", Smol),
            ):
                await vm.close()
            self.assertFalse(marker.exists())

    async def test_failed_marker_cancels_boot_and_destroys_its_vm(self):
        cancelled, destroyed = [], []

        @dataclass
        class Resources:
            storage_gb: int = 20
            overlay_gb: int = 20

        class Backend:
            def __init__(self, **kwargs):
                self._resources = Resources()
                self.ref = SimpleNamespace(id="pydantic-ai-boot")

        class Workspace:
            def __init__(self, backend):
                pass

            async def working_dir(self):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.append(True)

        class Smol:
            async def destroy(self, ref):
                destroyed.append(ref.id)

        vm = VmSandbox()
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("pydantic_ai.workspaces.Workspace", Workspace),
            patch("pydantic_ai_harness.smol_sandbox.SmolSandboxBackend", Backend),
            patch("pydantic_ai_harness.smol_sandbox.SmolSandbox", Smol),
            patch("vikram.sandbox._sweep", AsyncMock()),
            patch("vikram.sandbox._markers", return_value=Path(temporary)),
            patch(
                "vikram.sandbox.shutil.disk_usage",
                return_value=SimpleNamespace(free=10**13),
            ),
            patch.object(vm, "_mark", side_effect=PermissionError("marker denied")),
        ):
            with self.assertRaises(SandboxUnavailable):
                await vm._start()
        self.assertEqual(cancelled, [True])
        self.assertEqual(destroyed, ["pydantic-ai-boot"])
        self.assertIsNone(vm._boot)


if __name__ == "__main__":
    unittest.main()
