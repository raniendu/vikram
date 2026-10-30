import contextlib
import hashlib
import io
import json
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx
from test_installation import app_bundle

from vikram import cli, installation, releases
from vikram.installation import InstallError, Layout


def release_data(payload=b"package", version="0.2.0"):
    name = f"Vikram-{version}-macos-arm64.zip"
    return {
        "tag_name": f"v{version}",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": name,
                "size": len(payload),
                "state": "uploaded",
                "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "browser_download_url": f"https://github.com/raniendu/vikram/releases/download/v{version}/{name}",
            }
        ],
    }


def archive_bytes(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content, mode in entries:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = mode << 16
            archive.writestr(info, content)
    return output.getvalue()


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vikram-update-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Never the account running the tests: `vikram update` changes the
        # installation it belongs to. Tests that need a layout patch their own.
        self.layout = Layout(self.root / "default-account")
        guard = patch.object(releases, "runtime_layout", return_value=self.layout)
        guard.start()
        self.addCleanup(guard.stop)

    def client(self, handler):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    def test_latest_stable_with_verified_download_and_allowed_redirect(self):
        body = b"archive bytes"
        metadata = release_data(body)
        requested = []

        def handler(request):
            requested.append(request)
            if request.url.host == "api.github.com":
                return httpx.Response(200, json=metadata)
            if request.url.host == "github.com":
                return httpx.Response(
                    302,
                    headers={
                        "location": "https://release-assets.githubusercontent.com/file?signature=opaque"
                    },
                )
            return httpx.Response(200, content=body)

        client = self.client(handler)
        release = releases.latest_release(client)
        self.assertEqual(release.version, "0.2.0")
        target = releases.download_release(client, release, self.root / "download.zip")
        self.assertEqual(target.read_bytes(), body)
        self.assertEqual(len(requested), 3)
        self.assertTrue(all("authorization" not in r.headers for r in requested))

    def test_missing_release_and_rate_limit_are_distinct(self):
        self.assertIsNone(
            releases.latest_release(self.client(lambda r: httpx.Response(404)))
        )
        with self.assertRaisesRegex(InstallError, "rate|GitHub"):
            releases.latest_release(self.client(lambda r: httpx.Response(403)))

    def test_rejects_release_metadata_that_cannot_be_trusted(self):
        cases = [
            {"prerelease": True},
            {"draft": True},
            {"tag_name": "v0.2.0-beta"},
            {"tag_name": "../../evil"},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(InstallError):
                releases.parse_release({**release_data(), **changes})
        for field, value in [
            ("digest", None),
            ("size", 0),
            ("size", 300 * 1024**2),
            ("browser_download_url", "http://github.com/raniendu/vikram/file"),
            ("browser_download_url", "https://evil.example/file"),
            ("browser_download_url", "https://github.com/another/repo/file"),
            ("state", "new"),
            ("name", "Vikram-0.2.0-macos-x86.zip"),
        ]:
            metadata = release_data()
            metadata["assets"][0][field] = value
            with (
                self.subTest(field=field, value=value),
                self.assertRaises(InstallError),
            ):
                releases.parse_release(metadata)

    def test_corrupt_or_truncated_download_is_removed(self):
        release = releases.parse_release(release_data(b"correct"))
        destination = self.root / "download.zip"
        for content in [b"corrupt", b"short", b"over the expected size"]:
            with self.subTest(content=content), self.assertRaises(InstallError):
                releases.download_release(
                    self.client(lambda r: httpx.Response(200, content=content)),
                    release,
                    destination,
                )
            self.assertFalse(destination.exists())

    def test_rejects_untrusted_redirect_before_contacting_host(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

        with self.assertRaises(InstallError):
            releases.download_release(
                self.client(handler),
                releases.parse_release(release_data()),
                self.root / "a.zip",
            )
        self.assertEqual(len(requests), 1)

    def test_safe_archive_extracts_payload_and_executable_permissions(self):
        name = "Install Vikram.app/Contents/Resources/Vikram.app/Contents/MacOS/vikram-sidecar"
        source = self.root / "release.zip"
        source.write_bytes(archive_bytes([(name, b"binary", stat.S_IFREG | 0o755)]))
        app = releases.extract_release(source, self.root / "unpacked")
        binary = app / "Contents/MacOS/vikram-sidecar"
        self.assertEqual(binary.read_bytes(), b"binary")
        self.assertEqual(stat.S_IMODE(binary.stat().st_mode), 0o755)

    def test_unsafe_zip_entries_never_escape_extraction(self):
        prefix = "Install Vikram.app/Contents/"
        cases = [
            [("../escaped", b"bad", stat.S_IFREG | 0o644)],
            [(prefix + "../../../../escaped", b"bad", stat.S_IFREG | 0o644)],
            [("/tmp/escaped", b"bad", stat.S_IFREG | 0o644)],
            [(prefix + "link", b"/tmp", stat.S_IFLNK | 0o777)],
            [
                (prefix + "file", b"a", stat.S_IFREG | 0o644),
                (prefix + "FILE", b"b", stat.S_IFREG | 0o644),
            ],
            [
                (prefix + "a", b"a", stat.S_IFREG | 0o644),
                (prefix + "a/b", b"b", stat.S_IFREG | 0o644),
            ],
            [
                (prefix + "Implicit/a", b"a", stat.S_IFREG | 0o644),
                (prefix + "implicit/b", b"b", stat.S_IFREG | 0o644),
            ],
        ]
        for index, entries in enumerate(cases):
            source = self.root / f"bad{index}.zip"
            source.write_bytes(archive_bytes(entries))
            with (
                self.subTest(entries=entries),
                self.assertRaisesRegex(InstallError, "unsafe|conflicting"),
            ):
                releases.extract_release(source, self.root / f"unpacked{index}")
        self.assertFalse((self.root / "escaped").exists())

    def test_check_never_installs_or_creates_state(self):
        layout = Layout(self.root / "unused-account")
        client = self.client(lambda r: httpx.Response(200, json=release_data()))
        with (
            patch.object(releases, "runtime_layout", return_value=layout),
            patch.object(releases, "check_platform"),
            patch.object(releases, "make_client", return_value=client),
            patch.object(releases, "install_bundle") as install,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(releases.update_main(["--check"]), 0)
            self.assertIn("0.2.0", output.getvalue())
            install.assert_not_called()
        self.assertFalse(layout.root.exists())

    def test_update_installs_verified_release_and_keeps_previous_bundle(self):
        self.assert_update_pipeline()

    def test_corrupt_release_cannot_replace_installed_version(self):
        self.assert_update_pipeline(corrupt=True)

    def test_mismatched_payload_version_cannot_replace_installed_version(self):
        self.assert_update_pipeline(mismatched=True)

    def assert_update_pipeline(self, *, corrupt=False, mismatched=False):
        home = self.root / "account"
        home.mkdir()
        layout = Layout(home)
        old = app_bundle(self.root / "old")
        new = app_bundle(self.root / "new", "0.3.0" if mismatched else "0.2.0")
        entries = [
            (
                "Install Vikram.app/Contents/Resources/Vikram.app/"
                + path.relative_to(new).as_posix(),
                path.read_bytes(),
                path.stat().st_mode,
            )
            for path in new.rglob("*")
            if path.is_file()
        ]
        payload = archive_bytes(entries)
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.host == "api.github.com":
                return httpx.Response(200, json=release_data(payload))
            return httpx.Response(200, content=b"corrupt" if corrupt else payload)

        client = self.client(handler)
        with (
            patch.object(releases, "runtime_layout", return_value=layout),
            patch.object(releases, "check_platform"),
            patch.object(releases, "ensure_space"),
            patch.object(releases, "make_client", return_value=client),
            patch.object(installation, "check_platform"),
            patch.object(installation, "ensure_space"),
            patch.object(installation, "verify_bundle"),
            patch.object(installation, "check_runtime"),
            patch.object(installation, "login_profile", return_value=None),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            installation.install_bundle(old, layout)
            previous = layout.current.resolve()
            result = releases.update_main([])
        self.assertTrue((previous / "Vikram.app").is_dir())
        self.assertEqual(len(requests), 2)
        if corrupt or mismatched:
            self.assertEqual(result, 1)
            self.assertEqual(layout.current.resolve(), previous)
            self.assertEqual(installation.installed_version(layout), "0.1.0")
        else:
            self.assertEqual(result, 0)
            self.assertNotEqual(layout.current.resolve(), previous)
            self.assertEqual(installation.installed_version(layout), "0.2.0")

    def test_current_or_older_release_never_downloads(self):
        for version in ("0.1.0", "0.0.9"):
            requested = []

            def handler(request):
                requested.append(request)
                return httpx.Response(200, json=release_data(version=version))

            with (
                patch.object(releases, "check_platform"),
                patch.object(releases, "installed_version", return_value="0.1.0"),
                patch.object(
                    releases, "make_client", return_value=self.client(handler)
                ),
                patch.object(releases, "repair_app") as repair,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(releases.update_main([]), 0)
                self.assertIn("up to date", output.getvalue())
                self.assertEqual(len(requested), 1)
                # Up to date: only the app folder is brought to the installed version.
                repair.assert_called_once_with(self.layout)

    def test_network_failure_does_not_invoke_install(self):
        with (
            patch.object(releases, "check_platform"),
            patch.object(releases, "installed_version", return_value="0.1.0"),
            patch.object(
                releases,
                "latest_release",
                side_effect=httpx.ConnectError("unreachable"),
            ),
            patch.object(releases, "install_bundle") as install,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as error,
        ):
            self.assertEqual(releases.update_main([]), 1)
            install.assert_not_called()
            self.assertIn("current version is unchanged", error.getvalue())

    def test_update_dispatch_preserves_literal_prompt_escape(self):
        with patch("vikram.releases.update_main", return_value=0) as update:
            self.assertEqual(cli.main(["update", "--check"]), 0)
            update.assert_called_once_with(["--check"])
        with patch("vikram.cli._chat") as chat:
            self.assertEqual(cli.main(["--", "update"]), 0)
            self.assertEqual(chat.call_args.args[1], "update")


if __name__ == "__main__":
    unittest.main()
