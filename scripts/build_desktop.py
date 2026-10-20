"""Build the native sidecar and, by default, the Tauri application bundle."""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check_running_build() -> None:
    desktop = ROOT / "apps/desktop/src-tauri"
    bundle = desktop / "target/release/bundle/macos/Vikram.app/Contents/MacOS"
    outputs = {
        (bundle / "vikram-desktop").resolve(),
        (bundle / "vikram-sidecar").resolve(),
        (desktop / "target/release/vikram-desktop").resolve(),
        (desktop / "target/release/vikram-sidecar").resolve(),
        (desktop / "binaries/vikram-sidecar-aarch64-apple-darwin").resolve(),
    }
    try:
        processes = subprocess.run(
            ["/bin/ps", "-axo", "comm="],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        raise SystemExit("Cannot check for a running development build.") from None
    if any(Path(p.strip()).resolve() in outputs for p in processes if p.strip()):
        raise SystemExit(
            "Quit the development copy of Vikram before rebuilding it. "
            "Replacing an open Python bundle can break its deferred imports. "
            "The installed app can remain open."
        )


def check_space() -> None:
    if shutil.disk_usage(ROOT).free < 24 * 1024**3:
        raise SystemExit(
            "Build needs at least 24 GiB free to protect the 20 GiB reserve."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sidecar-only", action="store_true")
    args = parser.parse_args()
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit(
            "The first release targets macOS on Apple Silicon. Use an arm64 Python."
        )
    check_running_build()
    check_space()
    desktop = ROOT / "apps/desktop"
    destination = desktop / "src-tauri/binaries"
    destination.mkdir(parents=True, exist_ok=True)
    # The Monty sandbox runs code in this worker binary, installed beside Python.
    monty = Path(sys.executable).parent / "monty"
    if not monty.is_file():
        raise SystemExit("The Monty sandbox binary is missing. Run `uv sync --locked`.")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--name",
            "vikram-sidecar-aarch64-apple-darwin",
            "--paths",
            str(ROOT),
            "--collect-data",
            "pydantic_ai",
            "--collect-data",
            "certifi",
            "--recursive-copy-metadata",
            "pydantic-ai-slim",
            "--collect-data",
            "genai_prices",
            "--collect-submodules",
            "pydantic_ai_harness.ask_user",
            "--collect-submodules",
            "pydantic_ai_harness.code_mode",
            # Monty's compiled extension imports pydantic_monty._binary at run time.
            "--collect-submodules",
            "pydantic_monty",
            "--add-binary",
            f"{monty}:.",
            "--copy-metadata",
            "pydantic",
            "--copy-metadata",
            "openai",
            "--copy-metadata",
            "vikram",
            "--distpath",
            str(destination),
            "--workpath",
            str(ROOT / "build/sidecar"),
            "--specpath",
            str(ROOT / "build"),
            str(ROOT / "scripts/desktop_sidecar.py"),
        ],
        cwd=ROOT,
        check=True,
        env={
            **os.environ,
            "PYINSTALLER_CONFIG_DIR": str(ROOT / "build/pyinstaller-cache"),
        },
    )
    sandbox = subprocess.run(
        [str(destination / "vikram-sidecar-aarch64-apple-darwin"), "--check-sandbox"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if sandbox.returncode != 0:
        raise SystemExit("The bundled Python sandbox failed its startup check.")
    check_space()
    if not args.sidecar_only:
        subprocess.run(
            ["npm", "run", "bundle"],
            cwd=desktop,
            check=True,
            env={**os.environ, "CARGO_BUILD_JOBS": "2"},
        )
        subprocess.run(
            [
                "/usr/bin/codesign",
                "--verify",
                "--deep",
                "--strict",
                str(desktop / "src-tauri/target/release/bundle/macos/Vikram.app"),
            ],
            check=True,
        )
        with tempfile.TemporaryDirectory(prefix="vikram-package-check-") as library:
            check = subprocess.run(
                [
                    str(
                        desktop
                        / "src-tauri/target/release/bundle/macos/Vikram.app/Contents/MacOS/vikram-sidecar"
                    )
                ],
                input=json.dumps({"v": 1, "id": "check", "method": "hello"}) + "\n",
                text=True,
                capture_output=True,
                timeout=20,
                env={**os.environ, "VIKRAM_CHAT_DATA_DIR": library},
            )
            try:
                reply = json.loads(check.stdout)
                valid = check.returncode == 0 and reply["result"]["protocol"] == 1
            except (ValueError, KeyError, TypeError):
                valid = False
            if not valid:
                raise SystemExit("The bundled chat service failed its startup check.")
        check_space()


if __name__ == "__main__":
    main()
