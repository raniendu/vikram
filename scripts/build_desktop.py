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
SIDECAR = "vikram-sidecar-aarch64-apple-darwin"
# PyInstaller re-signs the binaries it collects, which strips the hypervisor
# entitlement from smolvm's VM helper. Ship that one file as verbatim data.
VERBATIM_VM_HELPER = """
a.binaries = [b for b in a.binaries if b[0].rsplit("/", 1)[-1] != "smol-vmm"]
a.datas = [d for d in a.datas if d[0].rsplit("/", 1)[-1] != "smol-vmm"]
a.datas.append(("smol/smol-vmm", {helper!r}, "DATA"))
"""


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


def sidecar_spec(image: Path) -> Path:
    """Write the sidecar's PyInstaller spec, keeping the VM helper verbatim."""
    import smol

    helper = Path(smol.__file__).with_name("smol-vmm")
    if not helper.is_file():
        raise SystemExit("The sandbox VM helper is missing. Run `uv sync --locked`.")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller.utils.cliutils.makespec",
            "--onefile",
            "--name",
            SIDECAR,
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
            "pydantic_ai_harness.smol_sandbox",
            # The VM runtime: its native module, helper, libraries, and notices.
            "--collect-all",
            "smol",
            "--add-data",
            f"{image}:sandbox",
            "--copy-metadata",
            "pydantic",
            "--copy-metadata",
            "openai",
            "--copy-metadata",
            "vikram",
            "--specpath",
            str(ROOT / "build"),
            str(ROOT / "scripts/desktop_sidecar.py"),
        ],
        cwd=ROOT,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    spec = ROOT / "build" / f"{SIDECAR}.spec"
    text = spec.read_text()
    marker = "pyz = PYZ(a.pure)"
    if text.count(marker) != 1:
        raise SystemExit(
            "PyInstaller wrote an unexpected spec; check the VM helper step."
        )
    spec.write_text(
        text.replace(marker, VERBATIM_VM_HELPER.format(helper=str(helper)) + marker)
    )
    return spec


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
    # The sandbox VM boots this digest-pinned image, so code never needs the network.
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/fetch_sandbox_image.py")],
        cwd=ROOT,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    from vikram.sandbox import IMAGE_FILE

    spec = sidecar_spec(ROOT / "build/sandbox" / IMAGE_FILE)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(destination),
            "--workpath",
            str(ROOT / "build/sidecar"),
            str(spec),
        ],
        cwd=ROOT,
        check=True,
        env={
            **os.environ,
            "PYINSTALLER_CONFIG_DIR": str(ROOT / "build/pyinstaller-cache"),
        },
    )
    # Boots a VM from the bundled helper and image, without a model.
    sandbox = subprocess.run(
        [str(destination / SIDECAR), "--check-sandbox"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if sandbox.returncode != 0:
        raise SystemExit(
            "The bundled Python sandbox failed its startup check. "
            + sandbox.stderr.strip()[-300:]
        )
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
