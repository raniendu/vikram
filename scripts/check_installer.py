"""Smoke-test an installer archive in a disposable installation directory."""

import argparse
import os
import pty
import select
import subprocess
import tempfile
import time
from pathlib import Path

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
        home = root / "Account with spaces"
        home.mkdir()
        layout = Layout(home)
        version = install_bundle(source, layout)
        assert installed_version(layout) == version == bundle_version(layout.app)
        env = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
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
        install_bundle(source, layout)
        assert previous != layout.current.resolve()
        assert (previous / "Vikram.app").is_dir()
        assert not (home / "Library/Application Support/dev.raniendu.vikram").exists()
        print(
            f"Installed and reinstalled Vikram {version} in a disposable account directory."
        )
        print(
            "CLI startup/version/update help, interactive terminal, Python sandbox, browser, and desktop service startup passed. Owner installation and chats were not touched."
        )


if __name__ == "__main__":
    main()
