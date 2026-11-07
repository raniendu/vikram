"""Offline extension SDK, worker lifecycle, and atomic package activation checks."""

import asyncio
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vikram.settings import Settings


class Allow:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.calls = []

    async def approve(self, request):
        self.calls.append(request)
        return self.allowed


class ExtensionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "vikram-extension.json").write_text(
            json.dumps(
                {
                    "api_version": 1,
                    "name": "example",
                    "version": "1.0",
                    "entry": "example:register",
                }
            )
        )
        (self.source / "example.py").write_text("""
import os
from vikram.extension_sdk import Extension

def register(extension):
    extension.register_tool("echo", lambda args, context: {"output": args["text"], "ui": [{"type":"text","text":"visible"}]}, description="Echo supplied text", schema={"type":"object", "properties":{"text":{"type":"string"}}, "required":["text"], "additionalProperties":False})
    extension.register_tool("environment", lambda args, context: {"output": os.environ.get("OPENROUTER_API_KEY", "absent")}, description="Environment", schema={"type":"object"})
    extension.register_command("greet", lambda args, context: {"output":"hello"})
    extension.register_hook("reply_complete", lambda payload, context: {"output":"observed"})
    extension.register_setting("greeting", "hello", description="A greeting")
""")

    def tearDown(self):
        self.temp.cleanup()

    async def test_local_install_worker_tools_commands_hooks_and_remove(self):
        from vikram.extensions import ExtensionManager

        manager = ExtensionManager(
            Settings(root=self.root / "state"), interaction=Allow()
        )
        try:
            installed = await manager.install(str(self.source))
            self.assertEqual(installed["name"], "example")
            tools = await manager.list_tools()
            self.assertEqual(
                {tool["name"] for tool in tools},
                {"ext__example__echo", "ext__example__environment"},
            )
            response = await manager.call_tool(
                "ext__example__echo", {"text": "hello"}, call_id="once"
            )
            self.assertTrue(response["ok"])
            self.assertEqual(response["output"], "hello")
            self.assertEqual(response["ui"][0]["text"], "visible")
            self.assertEqual(
                await manager.call_tool(
                    "ext__example__echo", {"text": "hello"}, call_id="once"
                ),
                response,
            )
            self.assertFalse(
                (
                    await manager.call_tool(
                        "ext__example__echo", {"text": "changed"}, call_id="once"
                    )
                )["ok"]
            )
            manager.begin_reply()
            self.assertEqual(
                (
                    await manager.call_tool(
                        "ext__example__echo", {"text": "next reply"}, call_id="once"
                    )
                )["output"],
                "next reply",
            )
            self.assertEqual(
                (await manager.command("ext__example__greet", {}))["output"], "hello"
            )
            self.assertEqual(
                (await manager.hook("reply_complete", {}))[0]["output"], "observed"
            )
            await manager.remove("example")
            self.assertEqual(manager.status(), [])
        finally:
            await manager.close()

    async def test_environment_approval_and_plan_fail_closed(self):
        from vikram.extensions import ExtensionManager

        interaction = Allow(False)
        manager = ExtensionManager(
            Settings(root=self.root / "state"), interaction=interaction
        )
        try:
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}):
                await manager.install(str(self.source))
                denied = await manager.call_tool("ext__example__environment", {})
                self.assertFalse(denied["ok"])
                interaction.allowed = True
                allowed = await manager.call_tool("ext__example__environment", {})
                self.assertEqual(allowed["output"], "absent")
            manager.mode = "plan"
            self.assertFalse(
                (await manager.call_tool("ext__example__echo", {"text": "no"}))["ok"]
            )
            with self.assertRaises(ValueError):
                await manager.install(str(self.source))
        finally:
            await manager.close()

    async def test_failed_update_preserves_previous_environment(self):
        from vikram.extensions import ExtensionManager

        manager = ExtensionManager(
            Settings(root=self.root / "state"), interaction=Allow()
        )
        try:
            await manager.install(str(self.source))
            previous = manager.status()[0]["version"]
            (self.source / "example.py").write_text(
                "raise RuntimeError('private failure')"
            )
            with self.assertRaises(ValueError):
                await manager.update("example")
            self.assertEqual(manager.status()[0]["version"], previous)
            self.assertEqual(
                (await manager.call_tool("ext__example__echo", {"text": "kept"}))[
                    "output"
                ],
                "kept",
            )
        finally:
            await manager.close()

    async def test_cancellation_stops_worker_and_does_not_replay_call_id(self):
        from vikram.extensions import ExtensionManager

        (self.source / "example.py").write_text("""
import asyncio
from pathlib import Path
async def wait(arguments, context):
    Path(arguments["marker"]).write_text("started")
    await asyncio.sleep(60)
    return "done"
def register(extension):
    extension.register_tool("wait", wait, description="Wait", schema={"type":"object","properties":{"marker":{"type":"string"}},"required":["marker"]})
""")
        manager = ExtensionManager(
            Settings(root=self.root / "state"), interaction=Allow()
        )
        try:
            await manager.install(str(self.source))
            marker = self.root / "started"
            task = asyncio.create_task(
                manager.call_tool(
                    "ext__example__wait", {"marker": str(marker)}, call_id="once"
                )
            )
            async with asyncio.timeout(5):
                while not marker.exists():
                    await asyncio.sleep(0.01)
            pid = manager._workers["example"].process.pid
            with self.assertRaisesRegex(ValueError, "still running"):
                manager.begin_reply()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            result = await manager.call_tool(
                "ext__example__wait", {"marker": str(marker)}, call_id="once"
            )
            self.assertFalse(result["ok"])
            self.assertEqual(manager.status()[0]["status"], "failed")
        finally:
            await manager.close()

    async def test_other_instance_prevents_removing_live_environment(self):
        from vikram.extensions import ExtensionManager

        settings = Settings(root=self.root / "state")
        first = ExtensionManager(settings, interaction=Allow())
        second = ExtensionManager(settings, interaction=Allow())
        try:
            await first.install(str(self.source))
            await second.start()
            with self.assertRaisesRegex(ValueError, "another"):
                await first.remove("example")
            self.assertEqual(len(first.status()), 1)
            self.assertTrue(
                (await second.call_tool("ext__example__echo", {"text": "alive"}))["ok"]
            )
            await second.close()
            await first.remove("example")
            self.assertEqual(first.status(), [])
        finally:
            await first.close()
            await second.close()

    async def test_settings_restart_worker_and_disabled_package_stays_off(self):
        from vikram.extensions import ExtensionManager

        settings = Settings(root=self.root / "state")
        manager = ExtensionManager(settings, interaction=Allow())
        try:
            await manager.install(str(self.source))
            await manager.set_setting("example", "greeting", "Changed")
            self.assertEqual(
                (await manager.list_settings("example"))["greeting"]["value"], "Changed"
            )
            await manager.enable("example", False)
            await manager.close()
            manager = ExtensionManager(settings, interaction=Allow())
            await manager.start()
            self.assertEqual(await manager.list_tools(), [])
            self.assertEqual(manager.status()[0]["status"], "disabled")
        finally:
            await manager.close()


class RuntimeArchiveTests(unittest.TestCase):
    def test_archive_filter_rejects_traversal_and_escaping_symlink(self):
        from scripts.fetch_extension_python import extract_archive

        for kind in ("file", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive = root / "runtime.tar.gz"
                with tarfile.open(archive, "w:gz") as output:
                    info = tarfile.TarInfo(
                        "../escaped" if kind == "file" else "python/link"
                    )
                    if kind == "file":
                        info.size = 4
                        output.addfile(info, io.BytesIO(b"data"))
                    else:
                        info.type = tarfile.SYMTYPE
                        info.linkname = "../../escaped"
                        output.addfile(info)
                destination = root / "extracted"
                destination.mkdir()
                with self.assertRaises(tarfile.FilterError):
                    extract_archive(archive, destination)
                self.assertFalse((root / "escaped").exists())

    def test_frozen_runtime_never_uses_system_python(self):
        from vikram.extensions import runtime_python

        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "App.app/Contents/MacOS/vikram-sidecar"
            with (
                patch("sys.frozen", True, create=True),
                patch("sys.executable", str(executable)),
            ):
                with self.assertRaisesRegex(ValueError, "bundled"):
                    runtime_python()
                expected = (
                    executable.parent.parent / "Resources/extension-python/bin/python3"
                )
                expected.parent.mkdir(parents=True)
                expected.touch()
                self.assertEqual(runtime_python(), expected)

    def test_runtime_pin_is_exact_core_version_and_immutable_digest(self):
        from scripts.fetch_extension_python import manifest

        pin = manifest()
        self.assertEqual(
            pin["version"], ".".join(map(str, __import__("sys").version_info[:3]))
        )
        self.assertEqual(len(pin["sha256"]), 64)
        self.assertIn("/releases/download/20260901/", pin["url"])
