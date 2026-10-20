"""Build a local, self-contained installer for both Vikram interfaces."""

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

from build_desktop import ROOT, check_space

from vikram.installation import (
    bundle_size,
    bundle_version,
    check_platform,
    check_runtime,
    cli_version,
    verify_bundle,
    version_tuple,
)


def release_version() -> str:
    desktop = ROOT / "apps/desktop"
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    version_tuple(version)
    versions = {
        "installed Python package": cli_version(),
        "Tauri": json.loads((desktop / "src-tauri/tauri.conf.json").read_text())[
            "version"
        ],
        "frontend": json.loads((desktop / "package.json").read_text())["version"],
        "Rust": tomllib.loads((desktop / "src-tauri/Cargo.toml").read_text())[
            "package"
        ]["version"],
    }
    for component, actual in versions.items():
        if actual != version:
            raise SystemExit(
                f"Version mismatch: {component} is {actual}, expected {version}."
            )
    return version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="package an already built and validated app",
    )
    args = parser.parse_args()
    check_platform()
    check_space()
    version = release_version()
    if not args.skip_build:
        subprocess.run(
            [sys.executable, str(ROOT / "scripts/build_desktop.py")], check=True
        )
    source = ROOT / "apps/desktop/src-tauri/target/release/bundle/macos/Vikram.app"
    if bundle_version(source) != version:
        raise SystemExit(
            "The app is stale. Rebuild it with the current release version."
        )
    bundle_size(source)
    verify_bundle(source)
    check_runtime(source, version)
    destination = ROOT / "dist/installers"
    destination.mkdir(parents=True, exist_ok=True)
    cache = ROOT / "build/installer-module-cache"
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="vikram-installer-", dir=ROOT / "build"
    ) as temporary:
        installer = Path(temporary) / "Install Vikram.app"
        contents = installer / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        (contents / "Resources").mkdir()
        shutil.copytree(source, contents / "Resources/Vikram.app")
        shutil.copy2(
            source / "Contents/Resources/icon.icns", contents / "Resources/icon.icns"
        )
        plist = {
            "CFBundleIdentifier": "dev.raniendu.vikram.installer",
            "CFBundleName": "Install Vikram",
            "CFBundleDisplayName": "Install Vikram",
            "CFBundleExecutable": "vikram-installer",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": version,
            "CFBundleVersion": version,
            "CFBundleIconFile": "icon.icns",
            "LSMinimumSystemVersion": "13.0",
            "NSHighResolutionCapable": True,
        }
        with (contents / "Info.plist").open("wb") as stream:
            plistlib.dump(plist, stream)
        subprocess.run(
            [
                "/usr/bin/xcrun",
                "swiftc",
                "-swift-version",
                "5",
                "-O",
                "-target",
                "arm64-apple-macosx13.0",
                "-module-cache-path",
                str(cache),
                str(ROOT / "scripts/installer.swift"),
                "-o",
                str(contents / "MacOS/vikram-installer"),
            ],
            check=True,
        )
        subprocess.run(
            ["/usr/bin/codesign", "--force", "--sign", "-", str(installer)], check=True
        )
        verify_bundle(installer)
        archive = destination / f"Vikram-{version}-macos-arm64.zip"
        staged_archive = Path(temporary) / archive.name
        with zipfile.ZipFile(
            staged_archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6
        ) as output:
            for path in sorted(installer.rglob("*")):
                if path.is_file():
                    output.write(path, str(path.relative_to(installer.parent)))
        # Verify exactly the archive consumers receive, including executable modes.
        from vikram.releases import extract_release

        app = extract_release(staged_archive, Path(temporary) / "archive-check")
        verify_bundle(app.parents[2])
        verify_bundle(app)
        if (
            not stat.S_IMODE((app / "Contents/MacOS/vikram-sidecar").stat().st_mode)
            & 0o111
        ):
            raise SystemExit("The packaged runtime lost executable permissions.")
        with staged_archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        check_space()
        os.replace(staged_archive, archive)
        (destination / f"{archive.name}.sha256").write_text(
            f"{digest}  {archive.name}\n"
        )
        # Also retain the clickable local installer without needing to unzip it.
        local = destination / "Install Vikram.app"
        if local.exists():
            shutil.rmtree(local)
        shutil.copytree(installer, local)
    check_space()
    print(f"Installer: {destination / 'Install Vikram.app'}")
    print(f"Archive: {archive}")
    print(
        "Local ad-hoc signed build. Developer ID signing and notarization are required before public distribution."
    )


if __name__ == "__main__":
    main()
