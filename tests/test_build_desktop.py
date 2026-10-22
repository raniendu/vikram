import subprocess
import unittest
from unittest.mock import patch

from scripts import build_desktop


class BuildTests(unittest.TestCase):
    def test_build_rejects_running_output_before_changing_files(self):
        desktop = build_desktop.ROOT / "apps/desktop/src-tauri"
        bundle = desktop / "target/release/bundle/macos/Vikram.app/Contents/MacOS"
        for executable in (
            bundle / "vikram-desktop",
            bundle / "vikram-sidecar",
            desktop / "target/release/vikram-desktop",
            desktop / "target/release/vikram-sidecar",
            desktop / "binaries/vikram-sidecar-aarch64-apple-darwin",
        ):
            with self.subTest(executable=executable):
                processes = str(executable) + "\n"
                with patch.object(
                    build_desktop.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, processes),
                ):
                    with self.assertRaisesRegex(SystemExit, "Quit.*Vikram"):
                        build_desktop.check_running_build()

    def test_installed_version_does_not_block_building_new_version(self):
        processes = (
            "/Users/example/Library/Application Support/dev.raniendu.vikram.install/"
            "versions/0.1.0-old/Vikram.app/Contents/MacOS/vikram-sidecar\n"
        )
        with patch.object(
            build_desktop.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, processes),
        ):
            build_desktop.check_running_build()
