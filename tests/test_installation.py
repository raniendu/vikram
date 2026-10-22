import contextlib
import os
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vikram import installation


def app_bundle(parent, version="0.1.0"):
    app = parent / "Vikram.app"
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump(
            {
                "CFBundleIdentifier": "dev.raniendu.vikram",
                "CFBundleShortVersionString": version,
                "CFBundleExecutable": "vikram-desktop",
                "LSMinimumSystemVersion": "13.0",
            },
            stream,
        )
    for name in ("vikram-desktop", "vikram-sidecar"):
        binary = contents / "MacOS" / name
        binary.write_text("executable fixture")
        binary.chmod(0o755)
    return app


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vikram-install-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "account with spaces"
        self.home.mkdir()
        self.layout = installation.Layout(self.home)
        self.source = app_bundle(self.base / "source")
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(installation, "check_platform"))
        self.stack.enter_context(patch.object(installation, "ensure_space"))
        self.verify = self.stack.enter_context(
            patch.object(installation, "verify_bundle")
        )
        self.health = self.stack.enter_context(
            patch.object(installation, "check_runtime")
        )
        self.stack.enter_context(
            patch.object(installation, "login_profile", return_value=".zprofile")
        )

    def install(self, source=None):
        return installation.install_bundle(source or self.source, self.layout)

    def test_first_install_and_idempotent_repeat_share_cli_and_app(self):
        library = self.home / "Library/Application Support/dev.raniendu.vikram"
        library.mkdir(parents=True)
        (library / "chats.db").write_bytes(b"existing private chats")
        profile = self.home / ".zprofile"
        profile.write_bytes(b"export EXISTING=value\n")
        self.assertEqual(self.install(), "0.1.0")
        self.assertEqual(installation.installed_version(self.layout), "0.1.0")
        self.assertEqual(
            self.layout.app.resolve(), self.layout.current.resolve() / "Vikram.app"
        )
        self.assertIn(str(self.layout.current), self.layout.cli.read_text())
        self.assertIn('--cli "$@"', self.layout.cli.read_text())
        self.assertTrue(os.access(self.layout.cli, os.X_OK))
        self.install()
        self.assertEqual(profile.read_bytes().count(b"# >>> Vikram CLI >>>"), 1)
        self.assertTrue(profile.read_bytes().startswith(b"export EXISTING=value\n"))
        self.assertEqual((library / "chats.db").read_bytes(), b"existing private chats")

    def test_update_switches_both_and_keeps_running_version(self):
        self.install()
        old = self.layout.current.resolve()
        newer = app_bundle(self.base / "new", "0.2.0")
        self.install(newer)
        self.assertNotEqual(self.layout.current.resolve(), old)
        self.assertTrue((old / "Vikram.app").is_dir())
        self.assertEqual(installation.installed_version(self.layout), "0.2.0")
        with self.assertRaisesRegex(installation.InstallError, "downgrade"):
            self.install()
        self.assertEqual(installation.installed_version(self.layout), "0.2.0")

    def test_failed_validation_or_activation_leaves_previous_version(self):
        self.install()
        old = self.layout.current.resolve()
        newer = app_bundle(self.base / "new", "0.2.0")
        self.health.side_effect = installation.InstallError("Startup failed")
        with self.assertRaisesRegex(installation.InstallError, "Startup"):
            self.install(newer)
        self.assertEqual(self.layout.current.resolve(), old)
        self.health.side_effect = None
        with patch.object(installation, "activate", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.install(newer)
        self.assertEqual(self.layout.current.resolve(), old)
        self.assertFalse(list(self.layout.versions.glob(".stage-*")))

    def test_interrupt_after_atomic_switch_keeps_new_release_and_allows_retry(self):
        self.install()
        previous = self.layout.current.resolve()
        newer = app_bundle(self.base / "new", "0.2.0")
        activate = installation.activate

        def interrupted(layout, release):
            activate(layout, release)
            raise KeyboardInterrupt()

        with patch.object(installation, "activate", side_effect=interrupted):
            try:
                result = self.install(newer)
            except KeyboardInterrupt:
                result = None
        self.assertTrue(self.layout.app.is_dir())
        self.assertEqual(result, "0.2.0")
        self.assertEqual(installation.installed_version(self.layout), "0.2.0")
        self.assertTrue((previous / "Vikram.app").is_dir())
        self.assertEqual(self.install(newer), "0.2.0")

    def test_interrupt_before_atomic_switch_keeps_old_release(self):
        self.install()
        previous = self.layout.current.resolve()
        newer = app_bundle(self.base / "new", "0.2.0")
        with patch.object(installation, "activate", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.install(newer)
        self.assertEqual(self.layout.current.resolve(), previous)
        self.assertEqual(installation.installed_version(self.layout), "0.1.0")
        self.assertEqual(self.install(newer), "0.2.0")

    def test_migrates_uv_link_without_removing_environment(self):
        old = self.home / ".local/share/uv/tools/vikram/bin/vikram"
        old.parent.mkdir(parents=True)
        old.write_text("old CLI")
        self.layout.cli.parent.mkdir(parents=True)
        self.layout.cli.symlink_to(old)
        self.install()
        self.assertFalse(self.layout.cli.is_symlink())
        self.assertEqual(self.layout.legacy_cli.resolve(), old.resolve())
        self.assertEqual(old.read_text(), "old CLI")
        self.install()
        self.assertEqual(self.layout.legacy_cli.resolve(), old.resolve())

    def test_retry_after_interrupted_uv_migration_preserves_backup(self):
        old = self.home / ".local/share/uv/tools/vikram/bin/vikram"
        old.parent.mkdir(parents=True)
        old.write_text("old CLI")
        self.layout.cli.parent.mkdir(parents=True)
        self.layout.cli.symlink_to(old)
        self.layout.legacy_cli.symlink_to(old)
        self.install()
        self.assertEqual(self.layout.legacy_cli.resolve(), old.resolve())
        self.assertEqual(installation.installed_version(self.layout), "0.1.0")

    def test_failed_first_install_restores_launcher_and_profile(self):
        old = self.home / ".local/share/uv/tools/vikram/bin/vikram"
        old.parent.mkdir(parents=True)
        old.write_text("old CLI")
        self.layout.cli.parent.mkdir(parents=True)
        self.layout.cli.symlink_to(old)
        profile = self.home / ".zprofile"
        profile.write_bytes(b"export EXISTING=preserved\n")
        with patch.object(installation, "activate", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual(self.layout.cli.resolve(), old.resolve())
        self.assertEqual(profile.read_bytes(), b"export EXISTING=preserved\n")
        self.assertFalse(os.path.lexists(self.layout.app))
        self.assertFalse(os.path.lexists(self.layout.legacy_cli))

    def test_real_reserve_includes_pending_bytes_and_runtime_headroom(self):
        required = installation.RESERVE + installation.RUNTIME_SPACE + 1234
        # Exit the setup patch for this one check without creating any files.
        self.stack.close()
        with patch("vikram.installation.shutil.disk_usage") as usage:
            usage.return_value.free = required - 1
            with self.assertRaisesRegex(installation.InstallError, "20 GiB"):
                installation.ensure_space(self.home, 1234)
            usage.return_value.free = required
            installation.ensure_space(self.home, 1234)

    def test_unknown_cli_and_app_are_not_overwritten(self):
        self.layout.cli.parent.mkdir(parents=True)
        self.layout.cli.write_text("unknown user command")
        with self.assertRaisesRegex(installation.InstallError, "existing"):
            self.install()
        self.assertEqual(self.layout.cli.read_text(), "unknown user command")
        self.layout.cli.unlink()
        self.layout.app.mkdir(parents=True)
        (self.layout.app / "important").write_text("keep me")
        with self.assertRaisesRegex(installation.InstallError, "existing"):
            self.install()
        self.assertEqual((self.layout.app / "important").read_text(), "keep me")
        self.assertFalse(self.layout.current.exists())

    def test_disk_reserve_failure_does_not_activate(self):
        with patch.object(
            installation,
            "ensure_space",
            side_effect=installation.InstallError("20 GiB"),
        ):
            with self.assertRaisesRegex(installation.InstallError, "20 GiB"):
                self.install()
        self.assertFalse(self.layout.current.exists())
        self.assertFalse(self.layout.cli.exists())

    def test_one_install_at_a_time(self):
        with installation.install_lock(self.layout):
            with self.assertRaisesRegex(installation.InstallError, "already"):
                self.install()

    def test_symlinked_managed_directories_are_rejected(self):
        outside = self.base / "other app"
        outside.mkdir()
        self.layout.root.parent.mkdir(parents=True)
        self.layout.root.symlink_to(outside)
        with self.assertRaisesRegex(installation.InstallError, "symlink"):
            self.install()
        self.assertEqual(list(outside.iterdir()), [])

    def test_staged_bundle_rejects_links_and_mismatched_identity(self):
        (self.source / "Contents/Resources").symlink_to(self.home)
        with self.assertRaises(installation.InstallError):
            self.install()
        self.assertFalse(self.layout.current.exists())

    def test_profile_symlink_is_not_followed(self):
        original = self.base / "dotfiles"
        original.write_text("keep this")
        (self.home / ".zprofile").symlink_to(original)
        with self.assertRaisesRegex(installation.InstallError, "profile"):
            self.install()
        self.assertEqual(original.read_text(), "keep this")
        self.assertFalse(self.layout.current.exists())


if __name__ == "__main__":
    unittest.main()
