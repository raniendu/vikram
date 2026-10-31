import contextlib
import os
import plistlib
import sys
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
        # A folder of its own, not a link: macOS remembers where a link pointed.
        self.assertFalse(self.layout.app.is_symlink())
        self.assertEqual(installation.bundle_version(self.layout.app), "0.1.0")
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

    def test_app_folder_stays_in_place_and_follows_updates(self):
        self.install()
        folder = self.layout.app.stat().st_ino
        self.install(app_bundle(self.base / "new", "0.2.0"))
        # The Dock and Spotlight keep the same folder, now with the new app.
        self.assertEqual(self.layout.app.stat().st_ino, folder)
        self.assertEqual(installation.bundle_version(self.layout.app), "0.2.0")
        self.assertEqual(
            (self.layout.root / installation.APP_SOURCE).read_text(),
            os.readlink(self.layout.current),
        )
        self.assertFalse(list(self.layout.root.glob(".app-*")))
        self.assertFalse(installation.publish_app(self.layout))

    def test_replaces_the_link_earlier_versions_made(self):
        self.install()
        # As 0.3.0 and earlier left it: a link to `current`, no record.
        import shutil

        shutil.rmtree(self.layout.app)
        (self.layout.root / installation.APP_SOURCE).unlink()
        self.layout.app.symlink_to(self.layout.current / "Vikram.app")
        self.install(app_bundle(self.base / "new", "0.2.0"))
        self.assertFalse(self.layout.app.is_symlink())
        self.assertEqual(installation.bundle_version(self.layout.app), "0.2.0")

    def test_app_left_behind_is_repaired_later(self):
        self.install()
        with patch.object(installation, "_swap", side_effect=OSError("busy")):
            self.assertEqual(
                self.install(app_bundle(self.base / "new", "0.2.0")), "0.2.0"
            )
        self.assertEqual(installation.installed_version(self.layout), "0.2.0")
        self.assertEqual(installation.bundle_version(self.layout.app), "0.1.0")
        self.assertFalse(list(self.layout.root.glob(".app-*")))
        self.assertTrue(installation.publish_app(self.layout))
        self.assertEqual(installation.bundle_version(self.layout.app), "0.2.0")

    def test_swap_exchanges_folders(self):
        first, second = self.base / "first", self.base / "second"
        first.mkdir()
        second.mkdir()
        (first / "a").write_text("1")
        (second / "b").write_text("2")
        installation._swap(first, second)
        self.assertEqual((first / "b").read_text(), "2")
        self.assertEqual((second / "a").read_text(), "1")

    def test_runtime_from_the_app_folder_is_the_installed_runtime(self):
        self.install()
        binary = self.layout.app / "Contents/MacOS/vikram-sidecar"
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", str(binary)),
        ):
            layout = installation.runtime_layout()
            self.assertEqual(layout, self.layout)
            self.assertTrue(installation.installed_runtime(layout))
        elsewhere = self.base / "Vikram.app/Contents/MacOS/vikram-sidecar"
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", str(elsewhere)),
        ):
            self.assertFalse(installation.installed_runtime(self.layout))
        self.assertFalse(installation.installed_runtime(self.layout))

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


class PruneTests(unittest.TestCase):
    """Old versions are removed only when nothing can still need them."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vikram-prune-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "account"
        self.home.mkdir()
        self.layout = installation.Layout(self.home)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name in ("check_platform", "ensure_space", "verify_bundle"):
            self.stack.enter_context(patch.object(installation, name))
        self.stack.enter_context(patch.object(installation, "check_runtime"))
        self.stack.enter_context(
            patch.object(installation, "login_profile", return_value=None)
        )
        self.running, self.count = [], 0
        self.stack.enter_context(
            patch.object(
                installation, "running_executables", side_effect=lambda: self.running
            )
        )

    def install(self, version):
        self.count += 1
        source = app_bundle(self.base / f"source-{self.count}", version)
        installation.install_bundle(source, self.layout)
        return self.layout.current.resolve().name

    def folders(self):
        if not self.layout.versions.is_dir():
            return []
        return sorted(p.name for p in self.layout.versions.iterdir())

    def runtime(self, folder):
        real = self.layout.versions.resolve() / folder
        return real / "Vikram.app/Contents/MacOS/vikram-sidecar"

    def test_updates_keep_only_current_and_previous(self):
        first = self.install("0.1.0")
        second = self.install("0.2.0")
        self.assertEqual(self.folders(), sorted([first, second]))
        third = self.install("0.3.0")
        fourth = self.install("0.4.0")
        self.assertEqual(self.folders(), sorted([third, fourth]))
        self.assertEqual(installation.installed_version(self.layout), "0.4.0")
        # The launcher and app still work through `current`.
        self.assertEqual(installation.bundle_version(self.layout.app), "0.4.0")
        self.assertTrue((self.layout.current / "Vikram.app").is_dir())

    def test_reinstalls_of_one_version_keep_the_latest_two(self):
        installs = [self.install("0.1.0") for _ in range(4)]
        self.assertEqual(self.folders(), sorted(installs[-2:]))
        self.assertEqual(self.layout.current.resolve().name, installs[-1])

    def test_version_in_use_is_kept_until_a_later_repair(self):
        from vikram import releases

        first = self.install("0.1.0")
        # A CLI or scheduled run started through `current` while 0.1.0 was active.
        self.running = [self.runtime(first), Path("/usr/bin/true")]
        self.install("0.2.0")
        third = self.install("0.3.0")
        self.assertIn(first, self.folders())
        releases.repair_app(self.layout)
        self.assertIn(first, self.folders())
        self.running = []  # It finished; the next launch or update removes it.
        releases.repair_app(self.layout)
        self.assertNotIn(first, self.folders())
        self.assertIn(third, self.folders())

    def test_app_source_version_is_kept(self):
        first = self.install("0.1.0")
        # The app folder could not be updated since 0.1.0 (it was open, say).
        with patch.object(installation, "publish_app"):
            self.install("0.2.0")
            third = self.install("0.3.0")
        self.assertEqual(
            (self.layout.root / installation.APP_SOURCE).read_text(),
            f"versions/{first}",
        )
        self.assertIn(first, self.folders())
        self.assertIn(third, self.folders())

    def test_unlisted_processes_mean_nothing_is_deleted(self):
        first = self.install("0.1.0")
        self.install("0.2.0")
        with patch.object(
            installation, "running_executables", side_effect=OSError("denied")
        ):
            third = self.install("0.3.0")
        self.assertEqual(installation.installed_version(self.layout), "0.3.0")
        self.assertIn(first, self.folders())
        self.assertEqual(len(self.folders()), 3)
        with self.assertRaises(OSError):
            with patch.object(
                installation, "running_executables", side_effect=OSError("denied")
            ):
                installation.prune_versions(self.layout)
        self.assertEqual(installation.prune_versions(self.layout), [first])
        self.assertIn(third, self.folders())

    def test_unrecognized_entries_are_untouched_and_leftovers_cleared(self):
        self.install("0.1.0")
        versions = self.layout.versions
        (versions / "notes").mkdir()
        (versions / "0.0.1-short").mkdir()
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "keep").write_text("mine")
        (versions / f"0.0.1-{'a' * 32}").symlink_to(outside)
        leftover = versions / f"{installation.REMOVING}{'b' * 32}"
        (leftover / "Vikram.app").mkdir(parents=True)
        self.install("0.2.0")
        self.install("0.3.0")
        self.assertFalse(leftover.exists())
        self.assertTrue((versions / "notes").is_dir())
        self.assertTrue((versions / "0.0.1-short").is_dir())
        self.assertEqual((outside / "keep").read_text(), "mine")

    def test_nothing_pruned_without_a_valid_current_link(self):
        self.assertEqual(installation.prune_versions(self.layout), [])
        self.install("0.1.0")
        self.install("0.2.0")
        self.install("0.3.0")
        before = self.folders()
        self.layout.current.unlink()
        self.assertEqual(installation.prune_versions(self.layout), [])
        self.layout.current.symlink_to("elsewhere")
        self.assertEqual(installation.prune_versions(self.layout), [])
        self.assertEqual(self.folders(), before)


@unittest.skipUnless(sys.platform == "darwin", "macOS process listing")
class RunningExecutablesTests(unittest.TestCase):
    def test_lists_this_process(self):
        executables = installation.running_executables()
        self.assertIn(
            Path(sys.executable).resolve(), [p.resolve() for p in executables]
        )

    def test_process_started_through_current_names_its_version(self):
        import subprocess

        with tempfile.TemporaryDirectory(prefix="vikram-running-test-") as temp:
            home = Path(temp)
            layout = installation.Layout(home)
            folder = layout.versions / f"0.1.0-{'c' * 32}"
            binary = folder / "Vikram.app/Contents/MacOS/vikram-sidecar"
            binary.parent.mkdir(parents=True)
            source = Path(sys.executable).resolve()
            binary.write_bytes(source.read_bytes())
            binary.chmod(0o755)
            layout.current.symlink_to(folder.relative_to(layout.root))
            # A copied interpreter finds its standard library through PYTHONHOME.
            child = subprocess.Popen(
                [
                    str(layout.current / "Vikram.app/Contents/MacOS/vikram-sidecar"),
                    "-c",
                    "import sys, time; print('up', flush=True); time.sleep(30)",
                ],
                env={**os.environ, "PYTHONHOME": sys.base_prefix},
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            try:
                if child.stdout.readline().strip() != b"up":
                    self.skipTest("A copied interpreter could not start here.")
                self.assertIn(binary.resolve(), installation.running_executables())
            finally:
                child.kill()
                child.wait()
                child.stdout.close()
