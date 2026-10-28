"""Smoke-test an installer archive in a disposable installation directory."""

import argparse
import os
import plistlib
import pty
import select
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from scripts.third_party import check_notices
from vikram.installation import (
    Layout,
    bundle_version,
    check_platform,
    ensure_space,
    install_bundle,
    installed_version,
    verify_bundle,
)
from vikram.releases import extract_release
from vikram.schedules import Schedule, ScheduleStore, agent_for, ensure_agent


def check_terminal(cli: Path, env: dict[str, str]) -> None:
    """Exercise frozen terminal imports and input without making a model request."""
    master, slave = pty.openpty()
    process = None
    try:
        process = subprocess.Popen(
            [str(cli)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={**env, "TERM": "xterm-256color", "PROMPT_TOOLKIT_NO_CPR": "1"},
        )
        os.close(slave)
        slave = None

        def wait_for(marker: bytes):
            output = b""
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        output = (output + os.read(master, 8192))[-65536:]
                    except OSError:
                        break
                    if marker in output:
                        return
                if process.poll() is not None:
                    break
            raise AssertionError("The packaged interactive terminal did not respond.")

        wait_for(b"Ctrl+T thinking")
        os.write(master, b"/trace\r")
        wait_for(b"No thinking was returned")
        os.write(master, b"/exit\r")
        assert process.wait(timeout=10) == 0
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        os.close(master)
        if slave is not None:
            os.close(slave)


def check_scheduler(layout: Layout, root: Path, env: dict[str, str]) -> None:
    """Run a due task through the agent's own command, as launchd would.

    The disposable chat library and a model that is not installed keep it from
    calling a real model; the run must still finish with a safe failure. The
    agent is written for the disposable account and never loaded into launchd.
    """
    library = root / "Scheduled library"
    tasks = ScheduleStore(library)
    try:
        task = tasks.create(
            name="Installer check",
            prompt="Say hello.",
            schedule=Schedule(repeat="daily", time="08:00"),
            model="vikram-installer-check:0",
        )
        tasks.request_run(task["id"])
        agent = agent_for(layout.home, library)
        assert ensure_agent(tasks, agent, run=_launchctl) == "on"
        definition = plistlib.loads(agent.plist.read_bytes())
        assert definition["StartInterval"] == 60
        # launchd's minimal environment, plus the disposable library.
        subprocess.run(
            definition["ProgramArguments"],
            capture_output=True,
            check=True,
            timeout=180,
            env={
                "HOME": str(layout.home),
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "VIKRAM_CHAT_DATA_DIR": str(library),
                "PYINSTALLER_RESET_ENVIRONMENT": "1",
            },
        )
        [run] = tasks.task(task["id"])["runs"]
        assert run["status"] == "failed" and run["error"], run
        assert "Traceback" not in run["error"]
        # "Run now" is done; the daily schedule's next time is due later.
        due = int((library / "scheduler-next-due").read_text())
        assert due > time.time(), due
        # Nothing due: the shell exits without starting Python.
        started = time.monotonic()
        subprocess.run(definition["ProgramArguments"], check=True, timeout=10, env=env)
        assert time.monotonic() - started < 2
    finally:
        tasks.close()


def _launchctl(*args: str):
    """Stands in for launchctl: nothing is loaded, and loading succeeds."""
    return SimpleNamespace(returncode=0 if args[0] == "bootstrap" else 113)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    check_platform()
    ensure_space(args.archive.parent, 2 * 1024**3)
    with tempfile.TemporaryDirectory(prefix="vikram-installer-smoke-") as temporary:
        root = Path(temporary)
        source = extract_release(args.archive, root / "extracted")
        verify_bundle(source.parents[2])
        # The sandbox's third-party notices, and the list of the sources
        # published beside the archive.
        sources = check_notices(source, bundle_version(source))
        home = root / "Account with spaces"
        home.mkdir()
        layout = Layout(home)
        version = install_bundle(source, layout)
        assert installed_version(layout) == version == bundle_version(layout.app)
        assert check_notices(layout.app, version) == sources
        # A library path that must stay absent: the CLI opens chats or tasks
        # only when it needs them.
        unused = root / "Unused library"
        env = {
            **os.environ,
            "PYINSTALLER_RESET_ENVIRONMENT": "1",
            "VIKRAM_CHAT_DATA_DIR": str(unused),
        }
        for arguments in (["--version"], ["--help"], ["update", "--help"]):
            result = subprocess.run(
                [str(layout.cli), *arguments],
                text=True,
                capture_output=True,
                check=True,
                timeout=45,
                env=env,
            )
            if arguments == ["--version"]:
                assert result.stdout.strip() == f"Vikram {version}"
        for check in ("--check-sandbox", "--check-browser"):
            subprocess.run(
                [str(layout.app / "Contents/MacOS/vikram-sidecar"), check],
                capture_output=True,
                check=True,
                timeout=90,
                env=env,
            )
        previous = layout.current.resolve()
        check_terminal(layout.cli, env)
        assert not unused.exists()
        check_scheduler(layout, root, env)
        install_bundle(source, layout)
        assert previous != layout.current.resolve()
        # The reinstall kept the disposable agent, still through `current`.
        agent = plistlib.loads(
            (
                home / "Library/LaunchAgents/dev.raniendu.vikram.scheduler.plist"
            ).read_bytes()
        )
        assert str(layout.current) in agent["ProgramArguments"][5]
        assert (previous / "Vikram.app").is_dir()
        assert not (home / "Library/Application Support/dev.raniendu.vikram").exists()
        print(
            f"Installed and reinstalled Vikram {version} in a disposable account directory."
        )
        print(
            "CLI startup/version/update help, interactive terminal, Python sandbox, browser, scheduled-task runner (agent command, as launchd runs it), and desktop service startup passed. Owner installation, chats, and launchd were not touched."
        )
        files = sum(len(component["files"]) for component in sources["components"])
        print(
            f"Third-party notices are in the app; they list {files} source files "
            f"for {len(sources['components'])} components."
        )


if __name__ == "__main__":
    main()
