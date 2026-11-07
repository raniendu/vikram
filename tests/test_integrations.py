import asyncio
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vikram.coding import CodingSession
from vikram.integrations import oauth_login, start
from vikram.settings import Settings


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_start_does_not_overlap_worker_launches(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            coding = CodingSession(root, settings=Settings(root / "state", root))
            self.addAsyncCleanup(coding.close)

            class Manager:
                active = 0
                maximum = 0

                def __init__(self, *args, **kwargs):
                    pass

                async def start(self):
                    Manager.active += 1
                    Manager.maximum = max(Manager.maximum, Manager.active)
                    await asyncio.sleep(0.01)
                    Manager.active -= 1

            with (
                patch("vikram.mcp_client.MCPManager", Manager),
                patch("vikram.extensions.ExtensionManager", Manager),
            ):
                await asyncio.gather(start(coding), start(coding), start(coding))
            self.assertEqual(Manager.maximum, 1)

    async def test_loopback_oauth_rejects_wrong_state_then_accepts_one_callback(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        statuses = []

        async def request(state):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                f"GET /callback?code=fixture-code&state={state} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode()
            )
            await writer.drain()
            statuses.append(await reader.readline())
            await reader.read()
            writer.close()
            await writer.wait_closed()

        class Manager:
            _connections = {
                "fixture": SimpleNamespace(
                    config=SimpleNamespace(
                        redirect_uri=f"http://127.0.0.1:{port}/callback"
                    )
                )
            }

            async def login(self, name, redirect, callback, uri):
                await redirect("https://example.test/authorize?state=expected")
                await request("wrong")
                await request("expected")
                self.result = await callback()
                return True

        manager = Manager()
        with patch("webbrowser.open", return_value=True):
            self.assertTrue(await oauth_login(manager, "fixture"))
        self.assertIn(b"400", statuses[0])
        self.assertIn(b"200", statuses[1])
        self.assertEqual(manager.result, ("fixture-code", "expected"))


class StartupRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_first_connection_is_closed_and_recreated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            coding = CodingSession(root, settings=Settings(root / "state", root))
            self.addAsyncCleanup(coding.close)
            started = asyncio.Event()
            instances = []

            class MCP:
                def __init__(self, *args, **kwargs):
                    self.closed = False
                    instances.append(self)

                async def start(self):
                    if len(instances) == 1:
                        started.set()
                        await asyncio.Event().wait()

                async def close(self):
                    self.closed = True

            class Extensions:
                def __init__(self, *args, **kwargs):
                    pass

                async def start(self):
                    pass

            with (
                patch("vikram.mcp_client.MCPManager", MCP),
                patch("vikram.extensions.ExtensionManager", Extensions),
            ):
                first = asyncio.create_task(start(coding))
                await started.wait()
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                self.assertTrue(instances[0].closed)
                self.assertFalse(hasattr(coding, "mcp"))
                await start(coding)
                self.assertEqual(len(instances), 2)
                self.assertIs(coding.mcp, instances[1])
