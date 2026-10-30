"""Updates for the installed app: check when it opens, install in the background.

The CLI and the app share one installation, so installing here updates both,
and `vikram update` in a terminal updates the app too. Either way the open
window keeps running its version and offers to restart into the new one.
"""

import asyncio
import contextlib
import os
from typing import Callable

import httpx

from vikram.installation import (
    InstallError,
    Layout,
    cli_version,
    installed_runtime,
    installed_version,
    runtime_layout,
    version_tuple,
)
from vikram.releases import install_release, latest_release, make_client, repair_app


def running_version() -> str:
    """The open app's version, which the app passes to its service.

    The service can be newer than its window: an update replaces the app's
    files, and a reconnect then starts the new service in the old window.
    """
    value = os.environ.get("VIKRAM_APP_VERSION", "")
    try:
        version_tuple(value)
        return value
    except InstallError:
        return cli_version()


class Updater:
    """States: "unavailable" (a checkout or test), "idle", "checking",
    "installing", "current", "ready" (installed; restart to use it), "failed"."""

    def __init__(
        self,
        emit: Callable[[dict], None],
        layout: Layout | None = None,
        managed: bool | None = None,
        running: str | None = None,
        client: Callable[[], httpx.Client] = make_client,
        interval: float = 30.0,
    ):
        self.emit = emit
        self.layout = layout or runtime_layout()
        self.managed = (
            installed_runtime(self.layout) and "VIKRAM_CHAT_DATA_DIR" not in os.environ
            if managed is None
            else managed
        )
        self.running = running or running_version()
        self.client = client
        self.interval = interval
        self.state = "idle" if self.managed else "unavailable"
        self.version: str | None = None
        self.error: str | None = None
        self.task: asyncio.Task | None = None
        self.watcher: asyncio.Task | None = None

    def status(self) -> dict:
        return {
            "state": self.state,
            "running": self.running,
            "version": self.version,
            "error": self.error,
        }

    def _set(self, state: str, version: str | None = None, error: str | None = None):
        self.state, self.version, self.error = state, version, error
        self.emit({"v": 1, "event": "update", **self.status()})

    def _installed(self) -> str | None:
        try:
            return installed_version(self.layout)
        except InstallError:
            return None

    def _newer(self, version: str | None) -> bool:
        return version is not None and version_tuple(version) > version_tuple(
            self.running
        )

    def watch(self) -> None:
        """Notice an update installed elsewhere, e.g. by `vikram update`."""
        if self.managed and self.watcher is None:
            self.watcher = asyncio.create_task(self._watch())

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            if self.state in {"checking", "installing"}:
                continue
            installed = await asyncio.to_thread(self._installed)
            if self._newer(installed) and (
                self.state != "ready" or self.version != installed
            ):
                # Its app files may still be at the earlier version.
                with contextlib.suppress(InstallError, OSError):
                    await asyncio.to_thread(repair_app, self.layout)
                self._set("ready", installed)

    def check(self) -> dict:
        """Start a check (and an install, if there is a newer release)."""
        self.watch()
        if self.managed and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self._check())
        return self.status()

    async def _check(self) -> None:
        installed = await asyncio.to_thread(self._installed)
        # Bring the app folder to the installed version: after an install
        # whose app step failed, an update from the CLI while this window was
        # open, or an update by 0.3.0 or earlier, which kept a link there.
        with contextlib.suppress(InstallError, OSError):
            await asyncio.to_thread(repair_app, self.layout)
        if self._newer(installed):
            self._set("ready", installed)
            return
        self._set("checking")
        try:
            with self.client() as client:
                release = await asyncio.to_thread(latest_release, client)
                current = installed or self.running
                if release is None or version_tuple(release.version) <= version_tuple(
                    current
                ):
                    self._set("current")
                    return
                self._set("installing", release.version)
                await asyncio.to_thread(
                    install_release, client, self.layout, release, lambda _: None
                )
            installed = await asyncio.to_thread(self._installed)
        except InstallError as error:
            self._set("failed", error=str(error))
        except (httpx.HTTPError, OSError):
            self._set(
                "failed",
                error="Could not check for updates. Check your connection and try again.",
            )
        else:
            self._set("ready" if self._newer(installed) else "current", installed)

    async def close(self) -> None:
        for task in (self.watcher, self.task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
