"""Build the bundled Python service and, by default, the native Vikram.app.

The app is a Swift package (`apps/macos`). This script builds its release
executable, assembles the bundle around it with the service, the page script,
the icon and the third-party notices, signs nested code first and then the
bundle (Hardened Runtime on the app's own executable), and checks the result.
"""

import argparse
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "apps/macos"
# The assembled application; the installer packages this bundle.
APP = PACKAGE / "build/Vikram.app"
SIDECAR = "vikram-sidecar-aarch64-apple-darwin"
SIDECAR_DIST = ROOT / "build/sidecar-dist"
# PyInstaller re-signs the binaries it collects, which strips the hypervisor
# entitlement from smolvm's VM helper. Ship that one file as verbatim data.
VERBATIM_VM_HELPER = """
a.binaries = [b for b in a.binaries if b[0].rsplit("/", 1)[-1] != "smol-vmm"]
a.datas = [d for d in a.datas if d[0].rsplit("/", 1)[-1] != "smol-vmm"]
a.datas.append(("smol/smol-vmm", {helper!r}, "DATA"))
"""


def check_running_build() -> None:
    bundle = APP / "Contents/MacOS"
    outputs = {
        (bundle / "vikram-desktop").resolve(),
        (bundle / "vikram-sidecar").resolve(),
        (SIDECAR_DIST / SIDECAR).resolve(),
        (PACKAGE / ".build/out/Products/Release/vikram-desktop").resolve(),
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


def build_app(version: str, notices: Path) -> None:
    """Build the Swift executable and assemble, sign and verify Vikram.app."""
    subprocess.run(
        [
            "/usr/bin/xcrun",
            "swift",
            "build",
            "--package-path",
            str(PACKAGE),
            "--configuration",
            "release",
            "--product",
            "vikram-desktop",
        ],
        check=True,
    )
    products = subprocess.run(
        [
            "/usr/bin/xcrun",
            "swift",
            "build",
            "--package-path",
            str(PACKAGE),
            "--configuration",
            "release",
            "--show-bin-path",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    with (PACKAGE / "Resources/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    if info["CFBundleShortVersionString"] != version:
        raise SystemExit(
            f"apps/macos/Resources/Info.plist says {info['CFBundleShortVersionString']}, "
            f"expected {version}. Run scripts/bump_version.py."
        )
    staging = APP.with_name(".Vikram.app.staging")
    shutil.rmtree(staging, ignore_errors=True)
    contents = staging / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Resources").mkdir()
    shutil.copy2(PACKAGE / "Resources/Info.plist", contents / "Info.plist")
    (contents / "PkgInfo").write_text("APPL????")
    shutil.copy2(Path(products) / "vikram-desktop", contents / "MacOS/vikram-desktop")
    shutil.copy2(SIDECAR_DIST / SIDECAR, contents / "MacOS/vikram-sidecar")
    for name in ("AppIcon.icns", "browser.js"):
        shutil.copy2(PACKAGE / "Resources" / name, contents / "Resources" / name)
    shutil.copytree(notices, contents / "Resources/notices")
    # Nested code first, then the bundle. The Python service keeps plain ad-hoc
    # signing: its unpacked libraries are not signed by one team (docs/releases.md).
    subprocess.run(
        [
            "/usr/bin/codesign",
            "--force",
            "--sign",
            "-",
            str(contents / "MacOS/vikram-sidecar"),
        ],
        check=True,
    )
    subprocess.run(
        [
            "/usr/bin/codesign",
            "--force",
            "--sign",
            "-",
            "--options",
            "runtime",
            str(staging),
        ],
        check=True,
    )
    shutil.rmtree(APP, ignore_errors=True)
    staging.rename(APP)
    subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(APP)], check=True
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--sidecar-only",
        action="store_true",
        help="build only the bundled Python service",
    )
    args = parser.parse_args()
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit(
            "The first release targets macOS on Apple Silicon. Use an arm64 Python."
        )
    check_running_build()
    check_space()
    SIDECAR_DIST.mkdir(parents=True, exist_ok=True)
    # The sandbox VM boots this digest-pinned image, so code never needs the network.
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/fetch_sandbox_image.py")],
        cwd=ROOT,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    from scripts.third_party import (
        NOTICES,
        SourceError,
        build_notices,
        check_notices,
        project_version,
    )
    from vikram.sandbox import IMAGE_FILE

    # The notices for the sandbox's third-party parts, which the app bundles. They
    # also stop the build when the image or VM runtime outgrew the source list.
    version = project_version()
    try:
        build_notices(version)
    except SourceError as error:
        raise SystemExit(str(error)) from None
    spec = sidecar_spec(ROOT / "build/sandbox" / IMAGE_FILE)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(SIDECAR_DIST),
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
        [str(SIDECAR_DIST / SIDECAR), "--check-sandbox"],
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
    if args.sidecar_only:
        return
    build_app(version, NOTICES)
    try:
        check_notices(APP, version)
    except SourceError as error:
        raise SystemExit(str(error)) from None
    service = APP / "Contents/MacOS/vikram-sidecar"
    # The CLI's hidden browser: the bundled app in helper mode, reading a
    # blank page with its content blocker installed, without the network.
    browser = subprocess.run(
        [str(service), "--check-browser"],
        capture_output=True,
        text=True,
        timeout=90,
    )
    if browser.returncode != 0:
        raise SystemExit(
            "The bundled browser failed its startup check. "
            + browser.stderr.strip()[-300:]
        )
    with tempfile.TemporaryDirectory(prefix="vikram-package-check-") as library:
        check = subprocess.run(
            [str(service)],
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
    print(f"App: {APP}")


if __name__ == "__main__":
    main()
