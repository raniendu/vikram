import contextlib
import hashlib
import io
import json
import os
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx
import yaml

from scripts import publish_release, third_party
from scripts.publish_release import GitHub, PublishError
from vikram import releases

COMMIT = "c" * 40
REPO = "/repos/raniendu/vikram"
STORAGE = "release-assets.githubusercontent.com"


class FakeGitHub:
    """The release endpoints publishing uses, with drafts and the latest marker."""

    def __init__(self):
        self.refs = {"v0.2.0": {"type": "commit", "sha": COMMIT}}
        self.tag_objects = {}
        self.releases = []
        self.calls = []
        self.contents = {}
        # Whether drafts already report digests; GitHub computes them on upload.
        self.draft_digests = True
        self.wrong_digest = False
        self.corrupt = False
        # Corrupt only the stored copy of the asset with this name.
        self.corrupt_name = None
        self.names = {}
        self.storage_saw_token = False
        self.ids = iter(range(1, 1000))

    def client(self, test):
        client = httpx.Client(
            transport=httpx.MockTransport(self.handle),
            headers={"Authorization": "Bearer test-token"},
        )
        test.addCleanup(client.close)
        return GitHub(client)

    def find(self, release_id):
        return next(r for r in self.releases if r["id"] == release_id)

    def digest_of(self, asset_id):
        if self.wrong_digest:
            return "sha256:" + "0" * 64
        return "sha256:" + hashlib.sha256(self.contents[asset_id]).hexdigest()

    def add_release(self, tag="v0.2.0", draft=True, assets=()):
        release_id = next(self.ids)
        release = {
            "id": release_id,
            "tag_name": tag,
            "draft": draft,
            "prerelease": False,
            "latest": False,
            "assets": list(assets),
            "html_url": f"https://github.com/raniendu/vikram/releases/tag/{tag}",
            "upload_url": f"https://uploads.github.com{REPO}/releases/{release_id}/assets{{?name,label}}",
        }
        self.releases.insert(0, release)
        return release

    def handle(self, request):
        method, path = request.method, request.url.path
        self.calls.append((method, path))
        if request.url.host == "uploads.github.com":
            release = self.find(int(path.split("/")[-2]))
            asset_id = next(self.ids)
            self.contents[asset_id] = request.content
            name = request.url.params["name"]
            self.names[asset_id] = name
            asset = {
                "id": asset_id,
                "name": name,
                "size": len(request.content),
                "state": "uploaded",
                "digest": self.digest_of(asset_id) if self.draft_digests else None,
                "browser_download_url": f"https://github.com/raniendu/vikram/releases/download/untagged-1/{name}",
            }
            release["assets"].append(asset)
            return httpx.Response(201, json=asset)
        if request.url.host == STORAGE:
            self.storage_saw_token |= "authorization" in request.headers
            asset_id = int(path.split("/")[-1])
            content = self.contents[asset_id]
            corrupt = self.corrupt or self.names[asset_id] == self.corrupt_name
            return httpx.Response(200, content=b"corrupt" if corrupt else content)
        rest = path.removeprefix(REPO)
        if method == "GET" and rest.startswith("/git/ref/tags/"):
            target = self.refs.get(rest.removeprefix("/git/ref/tags/"))
            if target is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"object": target})
        if method == "GET" and rest.startswith("/git/tags/"):
            target = self.tag_objects[rest.removeprefix("/git/tags/")]
            return httpx.Response(200, json={"object": target})
        if rest == "/releases" and method == "GET":
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json=self.releases[(page - 1) * 100 :][:100])
        if rest == "/releases" and method == "POST":
            body = json.loads(request.content)
            release = self.add_release(body["tag_name"], body["draft"])
            release["created_with"] = body
            return httpx.Response(201, json=release)
        if rest.startswith("/releases/assets/"):
            asset_id = int(rest.split("/")[-1])
            if method == "GET":
                assert request.headers["accept"] == "application/octet-stream"
                location = f"https://{STORAGE}/assets/{asset_id}?signature=opaque"
                return httpx.Response(302, headers={"location": location})
            if method == "DELETE":
                for release in self.releases:
                    release["assets"] = [
                        a for a in release["assets"] if a["id"] != asset_id
                    ]
                return httpx.Response(204)
        if rest == "/releases/latest" and method == "GET":
            latest = [r for r in self.releases if not r["draft"] and r["latest"]]
            if not latest:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=latest[0])
        if re.fullmatch(r"/releases/\d+", rest):
            release = self.find(int(rest.split("/")[-1]))
            if method == "GET":
                return httpx.Response(200, json=release)
            if method == "PATCH":
                body = json.loads(request.content)
                release["draft"] = body["draft"]
                release["prerelease"] = body["prerelease"]
                if body.get("make_latest") == "true":
                    for other in self.releases:
                        other["latest"] = other is release
                for asset in release["assets"]:
                    asset["digest"] = asset["digest"] or self.digest_of(asset["id"])
                    asset["browser_download_url"] = (
                        "https://github.com/raniendu/vikram/releases/download/"
                        f"{release['tag_name']}/{asset['name']}"
                    )
                return httpx.Response(200, json=release)
        return httpx.Response(500, json={"message": f"unexpected {method} {path}"})

    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]


class PublishReleaseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="vikram-publish-test-")
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.archive = self.archive_for("0.2.0", b"installer archive bytes")
        self.release = publish_release.check_archive("v0.2.0", self.archive)
        self.github = FakeGitHub()

    def archive_for(self, version, payload, directory=None, notices=None):
        """An installer archive whose app carries third-party notices for version."""
        directory = directory or self.directory
        archive = directory / f"Vikram-{version}-macos-arm64.zip"
        root = releases.ARCHIVE_ROOT
        folder = f"{root}/Contents/Resources/Vikram.app/{third_party.APP_NOTICES}"
        notices = version if notices is None else notices
        sources = {
            "format": 1,
            "components": [
                {
                    "folder": "vm-runtime/linux-6.18.55",
                    "files": [
                        {
                            "name": "linux-6.18.55.tar.xz",
                            "url": "https://cdn.kernel.org/linux-6.18.55.tar.xz",
                            "size": 1,
                            "sha256": "0" * 64,
                        }
                    ],
                }
            ],
        }
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr(f"{root}/Contents/Info.plist", payload)
            if notices:
                output.writestr(
                    f"{folder}/{third_party.NOTICES_FILE}",
                    f"Vikram {notices} includes\n<{third_party.sources_url(notices)}>\n",
                )
                output.writestr(
                    f"{folder}/{third_party.SOURCES_FILE}", json.dumps(sources)
                )
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (directory / f"{archive.name}.sha256").write_text(f"{digest}  {archive.name}\n")
        return archive

    def extras(self):
        """The notices and sources assets a release publishes beside the archive."""
        notices = self.directory / third_party.notices_asset("0.2.0")
        notices.write_text("# Third-party notices\n")
        sources = self.directory / third_party.sources_asset("0.2.0")
        sources.write_bytes(b"sources tar " * 1000)
        return [(notices, "text/markdown"), (sources, "application/x-tar")]

    def publish(self, **options):
        with contextlib.redirect_stdout(io.StringIO()):
            return publish_release.publish(
                self.github.client(self),
                "v0.2.0",
                COMMIT,
                self.archive,
                self.release,
                delay=0,
                **options,
            )

    def test_archive_must_match_the_tag_checksum_and_updater_limits(self):
        self.assertEqual(self.release.version, "0.2.0")
        self.assertEqual(
            self.release.url,
            "https://github.com/raniendu/vikram/releases/download/v0.2.0/"
            "Vikram-0.2.0-macos-arm64.zip",
        )
        for tag in ("0.2.0", "v0.2", "v0.2.0-rc1", "v01.2.0"):
            with self.subTest(tag=tag):
                with self.assertRaisesRegex(PublishError, "not a vMAJOR"):
                    publish_release.check_archive(tag, self.archive)
        with self.assertRaisesRegex(PublishError, "needs Vikram-0.3.0"):
            publish_release.check_archive("v0.3.0", self.archive)
        with self.assertRaisesRegex(PublishError, "not the one the release Mac built"):
            publish_release.check_archive("v0.2.0", self.archive, "0" * 64)
        with patch.object(releases, "MAX_DOWNLOAD", 4):
            with self.assertRaisesRegex(PublishError, "would refuse"):
                publish_release.check_archive("v0.2.0", self.archive)
        checksum = self.directory / f"{self.archive.name}.sha256"
        checksum.write_text("0" * 64 + f"  {self.archive.name}\n")
        with self.assertRaisesRegex(PublishError, "does not match the archive"):
            publish_release.check_archive("v0.2.0", self.archive)

    def test_archive_must_carry_notices_for_its_version(self):
        for notices, message in (
            ("", "no third-party notices"),
            ("0.1.0", "not for Vikram 0.2.0"),
        ):
            with self.subTest(notices=notices):
                folder = self.directory / (notices or "none")
                folder.mkdir()
                archive = self.archive_for("0.2.0", b"app", folder, notices)
                with self.assertRaisesRegex(PublishError, message):
                    publish_release.check_archive("v0.2.0", archive)

    def test_publishes_a_verified_draft_and_only_then_marks_it_latest(self):
        page = self.publish()
        self.assertEqual(page, "https://github.com/raniendu/vikram/releases/tag/v0.2.0")
        self.assertEqual(
            self.github.writes(),
            [
                ("POST", f"{REPO}/releases"),
                ("POST", f"{REPO}/releases/1/assets"),
                ("POST", f"{REPO}/releases/1/assets"),
                ("PATCH", f"{REPO}/releases/1"),
            ],
        )
        (release,) = self.github.releases
        self.assertTrue(release["created_with"]["draft"])
        self.assertIn(self.release.sha256, release["created_with"]["body"])
        self.assertEqual(
            [asset["name"] for asset in release["assets"]],
            [self.archive.name, f"{self.archive.name}.sha256"],
        )
        # GitHub's copy was checked on the draft, before anyone could see it.
        calls = self.github.calls
        stored = ("GET", f"/assets/{release['assets'][0]['id']}")
        self.assertLess(
            calls.index(stored), calls.index(("PATCH", f"{REPO}/releases/1"))
        )
        self.assertFalse(self.github.storage_saw_token)
        self.assertFalse(release["draft"])
        self.assertTrue(release["latest"])
        self.assertEqual(releases.parse_release(release), self.release)

    def test_notices_and_sources_are_checked_before_the_release_is_visible(self):
        extras = self.extras()
        page = self.publish(extras=extras)
        self.assertEqual(page, "https://github.com/raniendu/vikram/releases/tag/v0.2.0")
        (release,) = self.github.releases
        self.assertEqual(
            [asset["name"] for asset in release["assets"]],
            [
                self.archive.name,
                f"{self.archive.name}.sha256",
                "Vikram-0.2.0-THIRD-PARTY-NOTICES.md",
                "Vikram-0.2.0-third-party-sources.tar",
            ],
        )
        calls = self.github.calls
        publishing = calls.index(("PATCH", f"{REPO}/releases/1"))
        for asset in release["assets"]:
            self.assertLess(calls.index(("GET", f"/assets/{asset['id']}")), publishing)
        body = release["created_with"]["body"]
        self.assertIn("`Vikram-0.2.0-third-party-sources.tar` holds", body)
        self.assertIn("`Vikram-0.2.0-THIRD-PARTY-NOTICES.md`", body)

    def test_a_corrupt_sources_copy_leaves_an_unpublished_draft(self):
        self.github.draft_digests = False
        self.github.corrupt_name = "Vikram-0.2.0-third-party-sources.tar"
        with self.assertRaisesRegex(
            PublishError, "copy of Vikram-0.2.0-third-party-sources.tar does not match"
        ):
            self.publish(extras=self.extras())
        self.assertNotIn("PATCH", [method for method, _ in self.github.calls])
        self.assertTrue(self.github.releases[0]["draft"])

    def test_annotated_tags_resolve_to_their_commit(self):
        self.github.refs["v0.2.0"] = {"type": "tag", "sha": "t" * 40}
        self.github.tag_objects["t" * 40] = {"type": "commit", "sha": COMMIT}
        self.publish()
        self.assertTrue(self.github.releases[0]["latest"])

    def test_moved_or_missing_tag_changes_nothing(self):
        self.github.refs["v0.2.0"] = {"type": "commit", "sha": "d" * 40}
        with self.assertRaisesRegex(PublishError, "no longer points"):
            self.publish()
        del self.github.refs["v0.2.0"]
        with self.assertRaisesRegex(PublishError, "HTTP 404"):
            self.publish()
        self.assertEqual(self.github.writes(), [])

    def test_never_replaces_a_published_release(self):
        self.github.add_release(draft=False)
        with self.assertRaisesRegex(PublishError, "already published"):
            self.publish()
        self.assertEqual(self.github.writes(), [])

    def test_drafts_without_digests_are_checked_by_content(self):
        self.github.draft_digests = False
        self.publish()
        release = self.github.releases[0]
        self.assertEqual(releases.parse_release(release), self.release)

    def test_wrong_digest_or_corrupt_copy_leaves_an_unpublished_draft(self):
        for fault in ("wrong_digest", "corrupt"):
            with self.subTest(fault=fault):
                self.github = FakeGitHub()
                self.github.draft_digests = fault == "wrong_digest"
                setattr(self.github, fault, True)
                with self.assertRaisesRegex(PublishError, "unpublished draft"):
                    self.publish()
                self.assertNotIn("PATCH", [method for method, _ in self.github.calls])
                self.assertTrue(self.github.releases[0]["draft"])

    def test_reuses_a_draft_from_an_earlier_attempt(self):
        stale = {"id": 500, "name": self.archive.name, "state": "starter"}
        draft = self.github.add_release(assets=[stale])
        self.publish()
        self.assertIn(("DELETE", f"{REPO}/releases/assets/500"), self.github.calls)
        self.assertNotIn(("POST", f"{REPO}/releases"), self.github.calls)
        self.assertEqual(len(draft["assets"]), 2)
        self.assertTrue(draft["latest"])

    def test_reports_when_latest_is_not_the_new_release(self):
        self.github.add_release("v0.1.0", draft=False)["latest"] = True
        original = self.github.handle

        def keep_old_latest(request):
            if request.method == "PATCH":
                request = httpx.Request(
                    "PATCH",
                    request.url,
                    json={"draft": False, "prerelease": False, "make_latest": "false"},
                )
            return original(request)

        self.github.handle = keep_old_latest
        with self.assertRaisesRegex(PublishError, "is published, but"):
            self.publish(attempts=2)

    def test_dry_run_reports_outputs_without_network(self):
        output = self.directory / "github-output"
        stdout = io.StringIO()
        with (
            patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}),
            patch("sys.argv", ["", "--dry-run", "--tag", "v0.2.0", str(self.archive)]),
            patch.object(httpx, "Client", side_effect=AssertionError("network")),
            contextlib.redirect_stdout(stdout),
        ):
            publish_release.main()
        self.assertIn("Dry run: nothing was published.", stdout.getvalue())
        self.assertEqual(
            output.read_text(), f"version=0.2.0\nsha256={self.release.sha256}\n"
        )
        with (
            patch.dict(os.environ, {"GITHUB_TOKEN": ""}),
            patch("sys.argv", ["", "--tag", "v0.2.0", str(self.archive)]),
        ):
            with self.assertRaisesRegex(SystemExit, "needs --commit and GITHUB_TOKEN"):
                publish_release.main()

    def test_main_publishes_with_the_token_and_reports_the_page(self):
        output = self.directory / "github-output"
        tokens = []
        real_client = httpx.Client

        def handle(request):
            tokens.append(request.headers.get("authorization"))
            return self.github.handle(request)

        def client(**options):
            return real_client(transport=httpx.MockTransport(handle), **options)

        prepared = []

        def release_assets(archive, version, destination, sources_archive=None):
            # The sources are checked before anything reaches GitHub.
            self.assertEqual(self.github.calls, [])
            self.assertEqual((archive, version), (self.archive, "0.2.0"))
            self.assertEqual(sources_archive, Path("mac/sources.tar"))
            prepared.append(destination)
            assets = []
            for path, kind in self.extras():
                assets.append((Path(path.replace(destination / path.name)), kind))
            return assets

        argv = ["", "--tag", "v0.2.0", "--commit", COMMIT, str(self.archive)]
        argv += ["--sources", "mac/sources.tar"]
        with (
            patch.dict(
                os.environ, {"GITHUB_OUTPUT": str(output), "GITHUB_TOKEN": "secret"}
            ),
            patch("sys.argv", argv + ["--sha256", self.release.sha256]),
            patch.object(httpx, "Client", side_effect=client),
            patch.object(publish_release.time, "sleep"),
            patch.object(third_party, "release_assets", side_effect=release_assets),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            publish_release.main()
        page = "https://github.com/raniendu/vikram/releases/tag/v0.2.0"
        self.assertEqual(output.read_text(), f"url={page}\n")
        self.assertTrue(self.github.releases[0]["latest"])
        self.assertEqual(len(self.github.releases[0]["assets"]), 4)
        # The downloaded sources lived only in a temporary folder.
        self.assertFalse(prepared[0].exists())
        # The token reaches GitHub's API but never its storage host.
        self.assertIn("Bearer secret", tokens)
        self.assertFalse(self.github.storage_saw_token)

    def test_unavailable_sources_stop_before_github(self):
        argv = ["", "--tag", "v0.2.0", "--commit", COMMIT, str(self.archive)]
        failure = third_party.SourceError("https://example.test/a returned HTTP 404.")
        with (
            patch.dict(os.environ, {"GITHUB_TOKEN": "secret"}),
            patch("sys.argv", argv),
            patch.object(httpx, "Client", side_effect=AssertionError("GitHub")),
            patch.object(third_party, "release_assets", side_effect=failure),
        ):
            with self.assertRaisesRegex(SystemExit, "Release stopped: .*HTTP 404"):
                publish_release.main()


class ReleaseWorkflowTests(unittest.TestCase):
    """Safety properties of the release workflow that review should not miss."""

    def setUp(self):
        workflows = Path(__file__).resolve().parents[1] / ".github/workflows"
        self.release = yaml.safe_load((workflows / "release.yml").read_text())
        self.ci = yaml.safe_load((workflows / "ci.yml").read_text())

    def test_only_a_version_tag_publishes(self):
        triggers = self.release[True]  # YAML reads the key `on` as true.
        self.assertEqual(set(triggers), {"push", "workflow_dispatch"})
        self.assertEqual(triggers["push"], {"tags": ["v[0-9]+.[0-9]+.[0-9]+"]})
        self.assertEqual(self.release["permissions"], {})
        publish = self.release["jobs"]["publish"]
        self.assertEqual(publish["needs"], "build")
        self.assertIn("github.event_name == 'push'", publish["if"])
        self.assertEqual(publish["permissions"], {"contents": "write"})

    def test_release_mac_builds_and_checks_without_write_access(self):
        build = self.release["jobs"]["build"]
        self.assertEqual(build["runs-on"][0], "self-hosted")
        self.assertIn("vikram-release", build["runs-on"])
        self.assertEqual(build["permissions"], {"contents": "read"})
        commands = "\n".join(step.get("run", "") for step in build["steps"])
        for command in (
            "scripts/build_installer.py",
            "scripts/check_installer.py",
            "scripts/publish_release.py --dry-run",
            'scripts/third_party.py sources "dist/installers/',
        ):
            self.assertIn(command, commands)
        # GitHub's runners never download the sources: the Mac hands them over.
        upload = next(
            s for s in build["steps"] if "upload-artifact" in s.get("uses", "")
        )
        self.assertIn("third-party-sources.tar", upload["with"]["path"])
        publish = "\n".join(
            step.get("run", "") for step in self.release["jobs"]["publish"]["steps"]
        )
        self.assertIn(
            '--sources "release/Vikram-$VERSION-third-party-sources.tar"', publish
        )
        self.assertIs(build["steps"][0]["with"]["persist-credentials"], False)

    def test_pull_requests_never_reach_the_release_mac(self):
        for job in self.ci["jobs"].values():
            self.assertNotIn("self-hosted", str(job["runs-on"]))
        self.assertNotIn("pull_request", self.release[True])

    def test_actions_are_pinned_to_commits(self):
        for job in self.release["jobs"].values():
            for step in job["steps"]:
                if "uses" in step:
                    self.assertRegex(step["uses"], r"@[0-9a-f]{40}$")
