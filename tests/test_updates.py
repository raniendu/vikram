import asyncio
import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from vikram.desktop import updates
from vikram.desktop.protocol import validate_command
from vikram.installation import InstallError, Layout
from vikram.releases import Release

RELEASE = Release("0.4.0", "https://github.com/x.zip", 10, "0" * 64)


class Client:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class UpdaterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="vikram-updates-test-")
        self.addCleanup(temp.cleanup)
        self.layout = Layout(Path(temp.name))
        self.events = []
        self.installed = "0.3.0"
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.latest = self.patch("latest_release", return_value=RELEASE)
        self.install = self.patch("install_release", side_effect=self.fake_install)
        self.repair = self.patch("repair_app")
        self.patch("installed_version", side_effect=lambda _: self.installed)

    def patch(self, name, **kwargs):
        return self.stack.enter_context(patch.object(updates, name, **kwargs))

    def fake_install(self, client, layout, release, say):
        self.installed = release.version
        return release.version

    def updater(self, **kwargs):
        options = dict(layout=self.layout, managed=True, running="0.3.0", client=Client)
        return updates.Updater(self.events.append, **{**options, **kwargs})

    async def finish(self, updater):
        updater.check()
        await updater.task
        await updater.close()
        return [(e["state"], e["version"]) for e in self.events]

    async def test_installs_a_newer_release_then_offers_a_restart(self):
        updater = self.updater()
        self.assertEqual(
            await self.finish(updater),
            [("checking", None), ("installing", "0.4.0"), ("ready", "0.4.0")],
        )
        self.install.assert_called_once()
        self.assertEqual(updater.status()["running"], "0.3.0")
        # Every check also brings the app folder up to the installed version.
        self.repair.assert_called_once_with(self.layout)

    async def test_up_to_date(self):
        self.latest.return_value = Release("0.3.0", RELEASE.url, 10, "0" * 64)
        self.assertEqual(
            await self.finish(self.updater()), [("checking", None), ("current", None)]
        )
        self.install.assert_not_called()

    async def test_installed_by_the_cli_needs_no_download(self):
        self.installed = "0.4.0"
        self.assertEqual(await self.finish(self.updater()), [("ready", "0.4.0")])
        self.latest.assert_not_called()
        # The app folder is brought up to the installed version if behind.
        self.repair.assert_called_once_with(self.layout)

    async def test_failures_keep_the_installed_version_and_say_why(self):
        self.latest.side_effect = httpx.ConnectError("offline")
        updater = self.updater()
        await self.finish(updater)
        self.assertEqual(updater.state, "failed")
        self.assertIn("connection", updater.error)
        self.latest.side_effect = None
        self.install.side_effect = InstallError("Another Vikram update is running.")
        self.events.clear()
        await self.finish(updater)
        self.assertEqual(updater.error, "Another Vikram update is running.")

    async def test_notices_an_update_from_the_terminal(self):
        updater = self.updater(interval=0.01)
        updater.watch()
        await asyncio.sleep(0.05)
        self.assertEqual(self.events, [])
        self.installed = "0.4.0"
        for _ in range(100):
            if self.events:
                break
            await asyncio.sleep(0.01)
        await updater.close()
        self.assertEqual(self.events[0]["state"], "ready")
        self.assertEqual(self.events[0]["version"], "0.4.0")
        self.assertEqual(len(self.events), 1)
        self.latest.assert_not_called()

    async def test_checkouts_and_tests_never_update(self):
        updater = self.updater(managed=False)
        self.assertEqual(updater.check()["state"], "unavailable")
        updater.watch()
        self.assertIsNone(updater.task)
        self.assertIsNone(updater.watcher)
        self.latest.assert_not_called()


class VersionTests(unittest.TestCase):
    def test_running_version_is_the_window_version(self):
        with patch.dict(os.environ, {"VIKRAM_APP_VERSION": "0.2.0"}):
            self.assertEqual(updates.running_version(), "0.2.0")
        with patch.dict(os.environ, {"VIKRAM_APP_VERSION": "not a version"}):
            self.assertEqual(updates.running_version(), updates.cli_version())

    def test_commands_take_no_parameters(self):
        for method in ("update_status", "check_update"):
            validate_command(f'{{"v":1,"id":"1","method":"{method}"}}')
            with self.assertRaises(ValueError):
                validate_command(
                    f'{{"v":1,"id":"1","method":"{method}","params":{{"x":1}}}}'
                )


if __name__ == "__main__":
    unittest.main()
