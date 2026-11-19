"""Persistent workspace contracts using a scripted native-backend boundary."""

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from pydantic_ai.workspaces import WorkspaceRef, WorkspaceUnavailableError
from test_desktop_runtime import ENTRY, TOOL_SHOW
from test_toolkit import Model, tool_call

from vikram import sandbox as vm
from vikram.cli import main
from vikram.context import activity_note
from vikram.conversation import stream_reply
from vikram.desktop.runtime import Runtime
from vikram.desktop.store import Store
from vikram.models import parse_model
from vikram.sandbox_session import load_state, reset_sandbox, save_state, state_path
from vikram.session import Session
from vikram.session_io import export_session, import_session

REAL_VM, REAL_SWEEP = vm.VmSandbox, vm._sweep


@dataclass
class Resources:
    storage_gb: int = 20
    overlay_gb: int = 20


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = state_path(self.root, "a" * 32)
        self.disks, self.backends, self.stops, self.deleted = {}, [], [], []
        self.fail_stop = False
        self.boot_gate: asyncio.Event | None = None
        self.boot_started = asyncio.Event()
        self.stop_gate: asyncio.Event | None = None
        self.stop_started = asyncio.Event()
        owner = self

        class Backend:
            def __init__(self, *, ref=None, **options):
                self.ref = ref
                self.options = options
                self._resources = Resources()
                owner.backends.append(self)

            async def get_sandbox(self):
                if self.ref is None:
                    self.ref = WorkspaceRef(
                        provider="smol", id="pydantic-ai-" + uuid4().hex
                    )
                    owner.disks[self.ref.id] = {}
                if self.ref.id not in owner.disks:
                    raise WorkspaceUnavailableError("The VM was deleted.")
                identity = self.ref.id

                async def stop():
                    owner.stop_started.set()
                    if owner.stop_gate is not None:
                        await owner.stop_gate.wait()
                    if owner.fail_stop:
                        raise RuntimeError("private engine detail")
                    owner.stops.append(identity)

                return SimpleNamespace(stop=stop)

        class Workspace:
            def __init__(self, backend=None):
                self.backend = backend

            @property
            def attached(self):
                return self.backend is not None

            @property
            def ref(self):
                return self.backend.ref if self.backend is not None else None

            async def working_dir(self):
                assert self.backend is not None
                await self.backend.get_sandbox()
                owner.boot_started.set()
                if owner.boot_gate is not None:
                    await owner.boot_gate.wait()
                return "/workspace"

            async def write_text(self, name, content):
                assert self.backend is not None
                owner.disks[self.backend.ref.id][name] = content

            async def run(self, argv, **options):
                assert self.backend is not None
                disk = owner.disks[self.backend.ref.id]
                # Scripted results, never execution of generated code on the host.
                code = disk["snippet.py"]
                if code == "write":
                    disk["result.txt"] = "retained"
                    output = "written\n"
                else:
                    output = disk.get("result.txt", "missing") + "\n"
                return SimpleNamespace(exit_code=0, stdout=output, stderr="")

        class Smol:
            async def destroy(self, ref):
                owner.deleted.append(ref.id)
                owner.disks.pop(ref.id, None)

        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for target, value in (
            ("vikram.sandbox.VmSandbox", REAL_VM),
            ("vikram.sandbox.unavailable_reason", lambda: None),
            ("vikram.sandbox._markers", lambda: self.root / "markers"),
            ("vikram.sandbox._sweep", AsyncMock()),
            ("pydantic_ai.workspaces.Workspace", Workspace),
            ("pydantic_ai_harness.smol_sandbox.SmolSandboxBackend", Backend),
            ("pydantic_ai_harness.smol_sandbox.SmolSandbox", Smol),
        ):
            self.stack.enter_context(patch(target, value))

    async def test_second_stop_still_waits_for_the_disk_flush(self):
        self.stop_gate = gate = asyncio.Event()
        model = Model(tool_call("run_code", {"code": "write"}), "Saved.")
        stream = stream_reply(
            "local:1",
            False,
            [],
            "Write",
            web="auto",
            sandbox_state=self.path,
            transport=httpx.MockTransport(model),
        )
        closing = None
        try:
            async with asyncio.timeout(3):
                async for event in stream:
                    if event.get("usage"):
                        break
                await self.stop_started.wait()
                closing = asyncio.create_task(stream.aclose())
                await asyncio.sleep(0)
                closing.cancel()
                await asyncio.sleep(0)
                self.assertFalse(closing.done())
                gate.set()
                with contextlib.suppress(asyncio.CancelledError):
                    await closing
            saved = load_state(self.path)
            assert saved is not None
            self.assertTrue(saved.saved)
            self.assertEqual(self.stops, [saved.ref.id])
        finally:
            gate.set()
            if closing is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await closing
            await stream.aclose()

    async def test_repeated_boot_cancellation_retains_ownership_until_flush(self):
        self.boot_gate = asyncio.Event()
        self.stop_gate = gate = asyncio.Event()
        sandbox = REAL_VM(state_path=self.path)
        running = asyncio.create_task(sandbox.run("write"))
        closing = None
        try:
            async with asyncio.timeout(3):
                await self.boot_started.wait()
                while not self.path.exists():
                    await asyncio.sleep(0)
                running.cancel()
                await self.stop_started.wait()
                running.cancel()
                closing = asyncio.create_task(sandbox.close())
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertFalse(running.done())
                self.assertFalse(closing.done())
                self.assertEqual(self.stops, [])
                with self.assertRaisesRegex(ValueError, "is in use"):
                    await reset_sandbox(self.path)
                self.assertEqual(self.deleted, [])
                pending = load_state(self.path)
                assert pending is not None
                self.assertFalse(pending.saved)
                gate.set()
                with self.assertRaises(asyncio.CancelledError):
                    await running
                await closing
            saved = load_state(self.path)
            assert saved is not None
            self.assertTrue(saved.saved)
            self.assertEqual(self.stops, [saved.ref.id])
            await reset_sandbox(self.path)
            self.assertFalse(self.path.exists())
        finally:
            self.boot_gate.set()
            gate.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            if closing is not None:
                await asyncio.gather(closing, return_exceptions=True)
            await sandbox.close()

    async def test_unsaved_reference_failure_destroys_only_the_new_vm(self):
        sandbox = REAL_VM(state_path=self.path)
        with (
            patch("vikram.sandbox.save_state", side_effect=PermissionError("private")),
            self.assertRaises(vm.SandboxUnavailable),
        ):
            await sandbox.run("write")
        self.assertFalse(self.path.exists())
        self.assertEqual(len(self.deleted), 1)
        self.assertEqual(self.disks, {})

    async def test_crash_sweep_retains_conversation_owned_disks(self):
        sandbox = REAL_VM(state_path=self.path)
        await sandbox.run("write")
        await sandbox.close()
        saved = load_state(self.path)
        assert saved is not None
        marker = self.root / "markers" / f"{saved.ref.id}.json"
        self.assertTrue(json.loads(marker.read_text())["persistent"])
        marker.write_text(json.dumps({"pid": 999_999_999, "persistent": True}))
        with patch.object(vm, "_swept", False):
            await REAL_SWEEP()
        self.assertTrue(marker.exists())
        self.assertIn(saved.ref.id, self.disks)
        self.assertEqual(self.deleted, [])

    async def test_crash_sweep_discards_non_object_markers_without_deleting_vms(self):
        markers = self.root / "markers"
        markers.mkdir()
        for value in (None, [], True, 42, "invalid marker"):
            marker = markers / f"pydantic-ai-{uuid4().hex}.json"
            marker.write_text(json.dumps(value))
        with patch.object(vm, "_swept", False):
            await REAL_SWEEP()
        self.assertEqual(list(markers.glob("*.json")), [])
        self.assertEqual(self.deleted, [])

    def test_library_json_errors_and_sql_identifiers_are_bounded(self):
        store = Store(self.root / "invalid-library")
        try:
            cid = store.create_chat("local:1")["id"]
            with self.assertRaises(ValueError):
                store._insert_record("chats; DROP TABLE chats", {"id": "bad"})
            with self.assertRaises(ValueError):
                store._insert_record("chats", {"id) VALUES('bad');--": "bad"})
            with store.db:
                store.db.execute(
                    "UPDATE chats SET thinking=? WHERE id=?",
                    ("private-invalid-json", cid),
                )
            with self.assertRaisesRegex(ValueError, "metadata is damaged") as raised:
                store.chat(cid)
            self.assertNotIn("private-invalid-json", str(raised.exception))
        finally:
            store.close()

    async def test_native_ref_survives_a_new_adapter_and_disk_is_flushed(self):
        first = REAL_VM(state_path=self.path)
        await first.run("write")
        before = load_state(self.path)
        assert before is not None
        self.assertFalse(before.saved)
        await first.close()
        saved = load_state(self.path)
        assert saved is not None
        self.assertTrue(saved.saved)
        self.assertEqual(self.stops, [saved.ref.id])
        self.assertEqual(self.deleted, [])
        second = REAL_VM(state_path=self.path)
        try:
            result = await second.run("read")
            self.assertEqual(result.output, "retained\n")
            self.assertEqual(self.backends[-1].ref, saved.ref)
            self.assertEqual(len(self.disks), 1)
            self.assertEqual(self.backends[0]._resources.storage_gb, 1)
            self.assertEqual(self.backends[0].options["cpus"], vm.VM_CPUS)
            self.assertNotIn("allow_internet_access", self.backends[0].options)
            self.assertNotIn("env", self.backends[0].options)
        finally:
            await second.close()

    async def test_missing_vm_is_not_silently_replaced(self):
        first = REAL_VM(state_path=self.path)
        await first.run("write")
        await first.close()
        self.disks.clear()
        second = REAL_VM(state_path=self.path)
        try:
            with self.assertRaisesRegex(vm.SandboxUnavailable, "not recreated"):
                await second.run("read")
            self.assertEqual(self.disks, {})
            self.assertTrue(self.path.exists())
        finally:
            with contextlib.suppress(vm.SandboxUnavailable):
                await second.close()

    async def test_failed_flush_retains_ref_and_warns_on_resume(self):
        first = REAL_VM(state_path=self.path)
        await first.run("write")
        self.fail_stop = True
        with self.assertRaisesRegex(vm.SandboxUnavailable, "safely saved"):
            await first.close()
        saved = load_state(self.path)
        assert saved is not None
        self.assertFalse(saved.saved)
        self.fail_stop = False
        second = REAL_VM(state_path=self.path)
        try:
            result = await second.run("read")
            self.assertIn("interrupted", result.notice or "")
            self.assertEqual(result.output, "retained\n")
        finally:
            await second.close()

    async def test_state_lock_prevents_simultaneous_attachment_and_reset(self):
        first = REAL_VM(state_path=self.path)
        await first.run("write")
        try:
            with self.assertRaisesRegex(ValueError, "is in use"):
                await reset_sandbox(self.path)
            self.assertEqual(self.deleted, [])
        finally:
            await first.close()
        await reset_sandbox(self.path)
        self.assertFalse(self.path.exists())
        self.assertEqual(len(self.deleted), 1)

    async def test_saved_chat_routes_the_same_ref_through_cli_and_desktop(self):
        store = Store(self.root / "library")
        cid = store.create_chat("local:1")["id"]
        model = Model(tool_call("run_code", {"code": "write"}), "Saved in the sandbox.")
        session = Session(
            "local:1",
            store=store,
            chat_id=cid,
            streamer=partial(stream_reply, transport=httpx.MockTransport(model)),
        )

        async def discover(*, include_cloud=False, force=False):
            return [parse_model(ENTRY, TOOL_SHOW)]

        session.catalog.refresh = discover
        try:
            events = [event async for event in session.reply("Remember this file")]
            self.assertTrue(events[-1]["activity"][0]["persistent"])
            self.assertIn("across replies", model.bodies[0]["messages"][0]["content"])
        finally:
            await session.close()
            store.close()
        # Reopen the library as the other interface, as after an app restart.
        store = Store(self.root / "library")
        model = Model(tool_call("run_code", {"code": "read"}), "The file is retained.")
        runtime = Runtime(
            store,
            lambda event: None,
            streamer=partial(stream_reply, transport=httpx.MockTransport(model)),
        )
        runtime.catalog.refresh = discover
        try:
            store.save_draft(cid, "Read the existing file", 10)
            await runtime.send(cid, "continue")
            assert runtime.task is not None
            await runtime.task
            attempt = store.chat(cid)["messages"][-1]["attempts"][-1]
            self.assertEqual(attempt["status"], "complete", attempt["error"])
            self.assertEqual(attempt["activity"][0]["output"], "retained")
            self.assertEqual(len(self.disks), 1)
        finally:
            await runtime.close()
            store.close()

    async def test_no_session_and_scheduled_runs_stay_ephemeral(self):
        for scheduled in (None, "Daily task"):
            model = Model(tool_call("run_code", {"code": "write"}), "Done")
            events = [
                event
                async for event in stream_reply(
                    "local:1",
                    False,
                    [],
                    "Compute",
                    web="auto",
                    scheduled=scheduled,
                    sandbox_state=self.path if scheduled else None,
                    transport=httpx.MockTransport(model),
                )
            ]
            self.assertFalse(events[-1]["activity"][0]["persistent"])
        self.assertFalse(self.path.exists())
        self.assertEqual(len(self.deleted), 2)

    async def test_save_failure_is_not_a_completed_desktop_answer(self):
        store = Store(self.root / "failure-library")
        model = Model(tool_call("run_code", {"code": "write"}), "Saved.")
        runtime = Runtime(
            store,
            lambda event: None,
            streamer=partial(stream_reply, transport=httpx.MockTransport(model)),
        )

        async def discover(*, include_cloud=False, force=False):
            return [parse_model(ENTRY, TOOL_SHOW)]

        runtime.catalog.refresh = discover
        self.fail_stop = True
        try:
            cid = store.create_chat("local:1")["id"]
            store.save_draft(cid, "Write", 1)
            await runtime.send(cid, "fail-save")
            assert runtime.task is not None
            await runtime.task
            attempt = store.chat(cid)["messages"][-1]["attempts"][-1]
            self.assertEqual(attempt["status"], "failed")
            self.assertIn("safely saved", attempt["error"])
            self.assertNotIn("private engine", attempt["error"])
        finally:
            self.fail_stop = False
            await runtime.close()
            store.close()

    async def test_reset_is_confirmed_and_deletion_does_not_orphan_a_vm(self):
        store = Store(self.root / "reset-library")
        cid = store.create_chat("local:1")["id"]
        path = state_path(store.root, cid)
        sandbox = REAL_VM(state_path=path)
        await sandbox.run("write")
        await sandbox.close()
        runtime = Runtime(store, lambda event: None)
        try:
            with self.assertRaisesRegex(ValueError, "Reset"):
                store.delete_chat(cid)
            with self.assertRaisesRegex(ValueError, "Confirm"):
                await runtime.workbench.command(
                    {"action": "sandbox_reset", "chat_id": cid}
                )
            await runtime.workbench.command(
                {
                    "action": "sandbox_reset",
                    "chat_id": cid,
                    "options": {"confirmed": True},
                }
            )
            self.assertFalse(path.exists())
            store.delete_chat(cid)
            self.assertEqual(len(self.deleted), 1)
        finally:
            await runtime.close()
            store.close()

    async def test_forks_and_exports_do_not_transfer_sandbox_authority(self):
        store = Store(self.root / "fork-library")
        try:
            cid = store.create_chat("local:1")["id"]
            sandbox = REAL_VM(state_path=state_path(store.root, cid))
            await sandbox.run("write")
            await sandbox.close()
            clone = store.clone_chat(cid)
            self.assertFalse(state_path(store.root, clone["id"]).exists())
            exported = export_session(store, cid)
            self.assertNotIn("pydantic-ai-", exported)
            imported = import_session(store, exported)
            self.assertFalse(state_path(store.root, imported["id"]).exists())
        finally:
            store.close()

    async def test_private_metadata_rejects_foreign_refs_and_oversized_files(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "provider": "smol",
                    "id": "another-app-vm",
                    "saved": True,
                }
            )
        )
        sandbox = REAL_VM(state_path=self.path)
        with self.assertRaisesRegex(vm.SandboxUnavailable, "invalid"):
            await sandbox.run("read")
        self.assertEqual(self.backends, [])
        self.assertTrue(self.path.exists())
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "provider": "smol",
                    "id": "pydantic-ai-" + "a" * 32,
                    "saved": True,
                    "padding": "x" * 4096,
                }
            )
        )
        with self.assertRaisesRegex(ValueError, "invalid"):
            load_state(self.path)
        with self.assertRaises(ValueError):
            state_path(self.root, "../other")
        with self.assertRaises(ValueError):
            save_state(self.path, WorkspaceRef(provider="remote", id="bad"), saved=True)

    async def test_cli_status_and_confirmed_reset_use_the_selected_library(self):
        store = Store(self.root / "cli-library")
        cid = store.create_chat("local:1")["id"]
        path = state_path(store.root, cid)
        sandbox = REAL_VM(state_path=path)
        await sandbox.run("write")
        await sandbox.close()
        store.close()
        args = ["--session", cid, "--session-dir", str(path.parent.parent)]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["sandbox", "status", *args]), 0)
            # Run the synchronous CLI on its own thread/event loop.
            self.assertEqual(
                await asyncio.to_thread(main, ["sandbox", "reset", *args, "--yes"]), 0
            )
        self.assertIn("saved and stopped", output.getvalue())
        self.assertFalse(path.exists())

    def test_context_distinguishes_persistent_and_old_disposable_runs(self):
        note = activity_note(
            [
                {
                    "kind": "code",
                    "status": "complete",
                    "code": "read",
                    "output": "retained",
                    "persistent": True,
                }
            ]
        )
        assert note is not None
        self.assertIn("may remain", note)
        self.assertNotIn("files were deleted", note)
