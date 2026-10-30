import gzip
import hashlib
import io
import json
import re
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx

from scripts import third_party
from scripts.third_party import SourceError
from vikram import releases
from vikram.sandbox import IMAGE_MANIFEST

COMMIT = "c" * 40
ALPINE = "https://gitlab.alpinelinux.org/alpine/aports/-/raw"
DISTFILES = "https://distfiles.alpinelinux.org/distfiles"


def package(name, version, license, origin, commit=COMMIT):
    return {
        "name": name,
        "version": version,
        "license": license,
        "origin": origin,
        "commit": commit,
    }


def tar_bytes(files: dict[str, bytes], compress: bool = False) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return gzip.compress(stream.getvalue()) if compress else stream.getvalue()


def make_image(path: Path, layers: list[dict[str, bytes]], env: list[str]) -> Path:
    """An image archive in the layout scripts/fetch_sandbox_image.py writes."""
    config = json.dumps({"config": {"Env": env}}).encode()
    blobs = {"sha256-config": config}
    for index, layer in enumerate(layers):
        blobs[f"sha256-layer{index}"] = tar_bytes(layer, compress=True)
    entry = {
        "Config": "sha256-config",
        "Layers": [f"sha256-layer{i}" for i in range(len(layers))],
    }
    path.write_bytes(
        tar_bytes({"manifest.json": json.dumps([entry]).encode(), **blobs})
    )
    return path


def make_smol(folder: Path) -> Path:
    (folder / "licenses").mkdir(parents=True)
    (folder / "THIRD_PARTY_NOTICES.md").write_text(
        "# Third-party notices\n\n- Source: "
        f"https://github.com/smol-machines/libkrunfw/tree/{'8' * 40}\n"
    )
    (folder / "licenses/linux-GPL-2.0-only.txt").write_text("GPL 2 text\n")
    (folder / "libkrunfw.5.dylib").write_bytes(b"kernel library")
    database = "P:tar\nV:1.35-r2\nL:GPL-3.0-or-later\no:tar\nc:" + "d" * 40 + "\n"
    (folder / "agent-rootfs.tar").write_bytes(
        tar_bytes(
            {
                "etc/alpine-release": b"3.19.0\n",
                "lib/apk/db/installed": database.encode(),
            }
        )
    )
    return folder


def sources_for(files: dict[str, bytes], **inputs) -> dict:
    """A small source list whose files the mock hosts serve."""
    return {
        "format": 1,
        "inputs": inputs,
        "image": {
            "reference": "docker.io/library/python:3.13-alpine",
            "platform": "linux/arm64",
            "python": "3.13.16",
            "alpine": "3.24.2",
            "packages": [
                package("busybox", "1.37.0-r31", "GPL-2.0-only", "busybox"),
                package("tzdata", "2026d-r0", "Public-Domain", "tzdata"),
                package("zlib", "1.3.2-r0", "Zlib", "zlib"),
            ],
        },
        "guest": {
            "alpine": "3.19.0",
            "packages": [package("tar", "1.35-r2", "GPL-3.0-or-later", "tar")],
        },
        "components": [
            {
                "folder": "python-image/busybox-1.37.0-r31",
                "name": "busybox",
                "version": "1.37.0-r31",
                "revision": COMMIT,
                "aports": "main/busybox",
                "licenses": ["GPL-2.0-only"],
                "for": "busybox",
                "files": [
                    {
                        "name": name,
                        "url": f"https://example.test/{name}",
                        "size": len(data),
                        **(
                            {"sha512": hashlib.sha512(data).hexdigest()}
                            if name.endswith(".bz2")
                            else {"sha256": hashlib.sha256(data).hexdigest()}
                        ),
                    }
                    for name, data in files.items()
                ],
            }
        ],
    }


FILES = {"APKBUILD": b"pkgver=1.37.0\npkgrel=31\n", "busybox-1.37.0.tar.bz2": b"BZh9"}


class Hosts:
    """Third-party hosts serving fixed files, recording every request."""

    def __init__(self, files: dict[str, bytes], heads: dict[str, dict] = None):
        self.files, self.heads, self.requests = files, heads or {}, []
        # Statuses or errors a URL returns before it answers normally.
        self.failures: dict[str, list] = {}

    def client(self, test) -> httpx.Client:
        client = httpx.Client(transport=httpx.MockTransport(self.handle))
        test.addCleanup(client.close)
        return client

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append((request.method, url))
        if self.failures.get(url):
            failure = self.failures[url].pop(0)
            if isinstance(failure, Exception):
                raise failure
            return httpx.Response(failure)
        if request.method == "HEAD" and url in self.heads:
            return httpx.Response(200, headers=self.heads[url])
        if url in self.files:
            data = self.files[url]
            if request.method == "HEAD":
                return httpx.Response(200, headers={"content-length": str(len(data))})
            return httpx.Response(200, content=data)
        return httpx.Response(404)


class ParsingTests(unittest.TestCase):
    def test_apk_database_lists_installed_packages_with_their_build(self):
        database = (
            "P:.python-rundeps\nV:20261001\nA:noarch\nL:\n\n"
            f"P:busybox\nV:1.37.0-r31\nA:aarch64\nL:GPL-2.0-only\no:busybox\nc:{COMMIT}\n"
            "F:bin\nR:busybox\nZ:Q1abc=\n\n"
            "P:alpine-keys\nV:2.6-r0\nL:MIT\no:alpine-keys\nc:" + "a" * 40 + "\n"
        )
        self.assertEqual(
            third_party.apk_packages(database),
            [
                package("alpine-keys", "2.6-r0", "MIT", "alpine-keys", "a" * 40),
                package("busybox", "1.37.0-r31", "GPL-2.0-only", "busybox"),
            ],
        )

    def test_gpl_lgpl_and_mpl_licenses_need_sources(self):
        for expression in (
            "GPL-2.0-only",
            "LGPL-2.1-or-later",
            "MPL-2.0 AND MIT",
            "BSD-3-Clause OR GPL-2.0-only",
            "AGPL-3.0-only",
        ):
            self.assertTrue(third_party.COPYLEFT.search(expression), expression)
        for expression in ("MIT", "Apache-2.0", "X11", "Zlib", "Public-Domain"):
            self.assertFalse(third_party.COPYLEFT.search(expression), expression)
        self.assertEqual(
            third_party.license_ids("(MIT OR Apache-2.0) AND GPL-2.0-only WITH X"),
            {"MIT", "Apache-2.0", "GPL-2.0-only", "X"},
        )

    def test_apkbuild_names_its_version_and_pins_every_source_file(self):
        first, second = "1" * 128, "2" * 128
        recipe = (
            'pkgname=busybox\npkgver=1.37.0\npkgrel=31\nsource="a b"\n'
            f'sha512sums="\n{first}  busybox-1.37.0.tar.bz2\n{second}  0001-fix.patch\n"\n'
        )
        self.assertEqual(third_party.apkbuild_version(recipe), "1.37.0-r31")
        self.assertEqual(
            third_party.apkbuild_checksums(recipe),
            [(first, "busybox-1.37.0.tar.bz2"), (second, "0001-fix.patch")],
        )
        self.assertEqual(third_party.apkbuild_checksums("pkgver=1\n"), [])

    def test_image_files_follow_layers_and_whiteouts(self):
        with tempfile.TemporaryDirectory() as folder:
            image = make_image(
                Path(folder) / "image.tar",
                [
                    {"etc/a": b"old", "etc/b": b"kept", "opt/x/y": b"gone"},
                    {"etc/a": b"new", "./opt/.wh.x": b"", "etc/.wh.c": b""},
                ],
                ["PYTHON_VERSION=3.13.16"],
            )
            config, files = third_party.image_files(image, lambda name: True)
        self.assertEqual(config["config"]["Env"], ["PYTHON_VERSION=3.13.16"])
        self.assertEqual(files, {"etc/a": b"new", "etc/b": b"kept"})


class RefreshTests(unittest.TestCase):
    def test_alpine_sources_come_from_aports_then_the_mirror(self):
        patch = b"--- a/fix\n+++ b/fix\n"
        recipe = (
            "pkgver=1.0\npkgrel=2\n"
            f'sha512sums="\n{"9" * 128}  foo-1.0.tar.gz\n'
            f'{hashlib.sha512(patch).hexdigest()}  fix.patch\n"\n'
        ).encode()
        base = f"{ALPINE}/{COMMIT}/community/foo"
        hosts = Hosts(
            {f"{base}/APKBUILD": recipe, f"{base}/fix.patch": patch},
            {f"{DISTFILES}/v3.24/foo-1.0.tar.gz": {"content-length": "1234"}},
        )
        packages = [
            package("bar", "1.0-r0", "MIT", "bar"),
            package("foo", "1.0-r2", "GPL-2.0-only", "foo"),
            package("foo-libs", "1.0-r2", "LGPL-2.1-or-later AND MIT", "foo"),
        ]
        [component] = third_party.alpine_components(
            hosts.client(self), packages, "3.24.2", "python-image"
        )
        self.assertEqual(component["folder"], "python-image/foo-1.0-r2")
        self.assertEqual(component["aports"], "community/foo")
        self.assertEqual(component["for"], "foo, foo-libs")
        self.assertEqual(
            component["licenses"], ["GPL-2.0-only", "LGPL-2.1-or-later", "MIT"]
        )
        self.assertEqual(
            component["files"],
            [
                {
                    "name": "APKBUILD",
                    "url": f"{base}/APKBUILD",
                    "size": len(recipe),
                    "sha256": hashlib.sha256(recipe).hexdigest(),
                },
                {
                    "name": "foo-1.0.tar.gz",
                    "url": f"{DISTFILES}/v3.24/foo-1.0.tar.gz",
                    "size": 1234,
                    "sha512": "9" * 128,
                },
                {
                    "name": "fix.patch",
                    "url": f"{base}/fix.patch",
                    "size": len(patch),
                    "sha512": hashlib.sha512(patch).hexdigest(),
                },
            ],
        )
        # Permissive packages need no source.
        self.assertNotIn(
            ("GET", f"{ALPINE}/{COMMIT}/main/bar/APKBUILD"), hosts.requests
        )

    def test_alpine_sources_must_match_the_installed_build(self):
        base = f"{ALPINE}/{COMMIT}/main/foo"
        hosts = Hosts({f"{base}/APKBUILD": b"pkgver=1.0\npkgrel=3\n"})
        with self.assertRaisesRegex(SourceError, "is not version 1.0-r2"):
            third_party.alpine_components(
                hosts.client(self),
                [package("foo", "1.0-r2", "GPL-2.0-only", "foo")],
                "3.24.2",
                "python-image",
            )
        mixed = [
            package("foo", "1.0-r2", "GPL-2.0-only", "foo"),
            package("foo-doc", "1.0-r1", "GPL-2.0-only", "foo"),
        ]
        with self.assertRaisesRegex(SourceError, "cannot tell"):
            third_party.alpine_components(hosts.client(self), mixed, "3.24.2", "x")

    def test_runtime_sources_pin_the_kernel_release_and_libkrunfw(self):
        library = "8" * 40
        kernel = "https://cdn.kernel.org/pub/linux/kernel/v6.x"
        archive = f"https://github.com/smol-machines/libkrunfw/archive/{library}.tar.gz"
        hosts = Hosts(
            {
                "https://raw.githubusercontent.com/smol-machines/libkrunfw/"
                f"{library}/Makefile": b"KERNEL_VERSION = linux-6.18.55\n"
                b"FULL_VERSION = 5.5.0\n",
                f"{kernel}/sha256sums.asc": f"{'7' * 64}  linux-6.18.55.tar.xz\n".encode(),
                archive: b"libkrunfw source",
            },
            {f"{kernel}/linux-6.18.55.tar.xz": {"content-length": "154779312"}},
        )
        with tempfile.TemporaryDirectory() as folder:
            linux, libkrunfw = third_party.runtime_components(
                hosts.client(self), make_smol(Path(folder))
            )
        self.assertEqual(linux["folder"], "vm-runtime/linux-6.18.55")
        self.assertEqual(
            linux["files"],
            [
                {
                    "name": "linux-6.18.55.tar.xz",
                    "url": f"{kernel}/linux-6.18.55.tar.xz",
                    "size": 154779312,
                    "sha256": "7" * 64,
                }
            ],
        )
        self.assertEqual(libkrunfw["revision"], library)
        self.assertEqual(libkrunfw["files"][0]["url"], archive)
        self.assertEqual(
            libkrunfw["files"][0]["sha256"],
            hashlib.sha256(b"libkrunfw source").hexdigest(),
        )

    def test_license_texts_follow_the_identifiers_in_use(self):
        spdx = third_party.SPDX
        hosts = Hosts({f"{spdx}/GPL-2.0-only.txt": b"GPL text"})
        with tempfile.TemporaryDirectory() as folder:
            texts = Path(folder)
            (texts / "Unused.txt").write_text("old")
            (texts / "MIT.txt").write_text("kept")
            third_party.refresh_license_texts(
                hosts.client(self),
                {"GPL-2.0-only", "MIT", "Public-Domain", "../escape"},
                texts,
            )
            self.assertEqual(
                sorted(path.name for path in texts.iterdir()),
                ["GPL-2.0-only.txt", "MIT.txt"],
            )
        # Kept texts are not fetched again, and odd identifiers never reach a URL.
        self.assertEqual(
            [url.rsplit("/", 1)[1] for _, url in hosts.requests],
            ["GPL-2.0-only.txt", "Public-Domain.txt"],
        )

    def test_the_guest_filesystem_is_read_from_smolmachines(self):
        with tempfile.TemporaryDirectory() as folder:
            alpine, packages = third_party.guest_filesystem(make_smol(Path(folder)))
        self.assertEqual(alpine, "3.19.0")
        self.assertEqual(
            packages, [package("tar", "1.35-r2", "GPL-3.0-or-later", "tar", "d" * 40)]
        )


class NoticesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="vikram-notices-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.smol = make_smol(self.root / "smol")
        metadata = b"Metadata-Version: 2.4\nName: pip\nVersion: 26.2.1\nLicense-Expression: MIT\n\nlong text"
        site = "usr/local/lib/python3.13/site-packages/pip-26.2.1.dist-info"
        self.image = make_image(
            self.root / "image.tar",
            [
                {"etc/alpine-release": b"3.24.2\n"},
                {
                    "usr/local/lib/python3.13/LICENSE.txt": b"PSF license",
                    f"{site}/METADATA": metadata,
                    f"{site}/licenses/AUTHORS.txt": b"authors",
                    f"{site}/licenses/LICENSE.txt": b"MIT license for pip",
                    f"{site}/licenses/src/pip/_vendor/rich/LICENSE": b"rich",
                    "usr/local/lib/python3.13/os.py": b"not a license",
                },
            ],
            ["PYTHON_VERSION=3.13.16"],
        )
        self.licenses = self.root / "texts"
        self.licenses.mkdir()
        for name in ("GPL-2.0-only", "GPL-3.0-or-later", "Zlib", "Unused"):
            (self.licenses / f"{name}.txt").write_text(f"{name} text\n")
        self.sources = sources_for(FILES, **third_party.inputs(self.smol))
        self.notices = self.root / "notices"

    def write(self, version="0.2.0"):
        third_party.write_notices(
            self.notices, version, self.sources, self.image, self.smol, self.licenses
        )
        return (self.notices / third_party.NOTICES_FILE).read_text()

    def test_notices_describe_every_component_and_carry_license_texts(self):
        document = self.write()
        self.assertEqual(third_party.check_folder(self.notices, "0.2.0"), self.sources)
        for expected in (
            "Vikram 0.2.0 includes",
            "<https://github.com/raniendu/vikram/releases/download/v0.2.0/"
            "Vikram-0.2.0-third-party-sources.tar>",
            "| `python-image/busybox-1.37.0-r31` | busybox | 1.37.0-r31 |",
            "Alpine Linux 3.19.0",
            "| tar | 1.35-r2 | GPL-3.0-or-later | tar |",
            "- CPython 3.13.16: [licenses/python-image/LICENSE.txt]",
            "- pip 26.2.1 (MIT): [licenses/python-image/pip-26.2.1.dist-info/"
            "licenses/LICENSE.txt]",
            "with the licenses of the packages it vendors beside it",
            "| Zlib | [licenses/Zlib.txt](licenses/Zlib.txt) |",
            "no standard license text: `Public-Domain`.",
            f"pinned to `{IMAGE_MANIFEST}`",
        ):
            self.assertIn(expected, document)
        files = {
            str(path.relative_to(self.notices))
            for path in self.notices.rglob("*")
            if path.is_file()
        }
        self.assertEqual(
            files,
            {
                "THIRD-PARTY-NOTICES.md",
                "third-party-sources.json",
                "licenses/GPL-2.0-only.txt",
                "licenses/GPL-3.0-or-later.txt",
                "licenses/Zlib.txt",
                "licenses/smolmachines/THIRD_PARTY_NOTICES.md",
                "licenses/smolmachines/licenses/linux-GPL-2.0-only.txt",
                "licenses/python-image/LICENSE.txt",
                "licenses/python-image/pip-26.2.1.dist-info/licenses/AUTHORS.txt",
                "licenses/python-image/pip-26.2.1.dist-info/licenses/LICENSE.txt",
                "licenses/python-image/pip-26.2.1.dist-info/licenses/src/pip/"
                "_vendor/rich/LICENSE",
            },
        )

    def test_rewriting_replaces_the_folder(self):
        self.write("0.1.0")
        (self.notices / "stale.txt").write_text("left over")
        self.write("0.2.0")
        self.assertFalse((self.notices / "stale.txt").exists())
        self.assertEqual(
            [path.name for path in self.root.iterdir() if path.name.startswith(".")],
            [],
        )

    def test_check_refuses_missing_stale_or_incomplete_notices(self):
        with self.assertRaisesRegex(SourceError, "no third-party notices"):
            third_party.check_folder(self.notices, "0.2.0")
        self.write()
        with self.assertRaisesRegex(SourceError, "not for Vikram 0.3.0"):
            third_party.check_folder(self.notices, "0.3.0")
        (self.notices / "licenses/Zlib.txt").unlink()
        with self.assertRaisesRegex(SourceError, "missing licenses/Zlib.txt"):
            third_party.check_folder(self.notices, "0.2.0")
        self.write()
        (self.notices / third_party.SOURCES_FILE).write_text('{"format": 1}')
        with self.assertRaisesRegex(SourceError, "malformed"):
            third_party.check_folder(self.notices, "0.2.0")
        (self.notices / third_party.NOTICES_FILE).write_bytes(b"\xff not text")
        with self.assertRaisesRegex(SourceError, "notices are malformed"):
            third_party.check_folder(self.notices, "0.2.0")

    def test_build_refuses_a_source_list_for_another_runtime_or_image(self):
        third_party.check_inputs(self.sources, self.smol)
        (self.smol / "libkrunfw.5.dylib").write_bytes(b"a newer kernel")
        with self.assertRaisesRegex(SourceError, "third_party.py refresh"):
            third_party.check_inputs(self.sources, self.smol)
        sources = sources_for(FILES, **third_party.inputs(self.smol))
        sources["inputs"]["image"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(SourceError, "changed since"):
            third_party.check_inputs(sources, self.smol)

    def test_notices_are_read_from_the_installer_archive(self):
        self.write()
        archive = self.root / "Vikram-0.2.0-macos-arm64.zip"
        prefix = f"{releases.ARCHIVE_ROOT}/Contents/Resources/Vikram.app/"
        with zipfile.ZipFile(archive, "w") as output:
            for path in sorted(self.notices.rglob("*")):
                if path.is_file():
                    name = f"{third_party.APP_NOTICES}/{path.relative_to(self.notices)}"
                    output.write(path, prefix + name)
        document, sources = third_party.archive_notices(archive, "0.2.0")
        self.assertEqual(document, self.write())
        self.assertEqual(sources, self.sources)
        with self.assertRaisesRegex(SourceError, "not for Vikram 0.3.0"):
            third_party.archive_notices(archive, "0.3.0")
        archive.write_bytes(b"not a zip")
        with self.assertRaisesRegex(SourceError, "could not be read"):
            third_party.archive_notices(archive, "0.2.0")


class SourcesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="vikram-sources-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sources = sources_for(FILES)
        self.hosts = Hosts(
            {f"https://example.test/{name}": data for name, data in FILES.items()}
        )
        pause = patch.object(third_party, "DELAY", 0)
        pause.start()
        self.addCleanup(pause.stop)

    def fetch(self, folder: str) -> Path:
        destination = self.root / folder
        destination.mkdir()
        return third_party.fetch_sources(
            self.hosts.client(self), self.sources, "0.2.0", destination
        )

    def test_sources_are_checked_and_packed_reproducibly(self):
        first, second = self.fetch("first"), self.fetch("second")
        self.assertEqual(first.name, "Vikram-0.2.0-third-party-sources.tar")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        prefix = "Vikram-0.2.0-third-party-sources"
        with tarfile.open(first) as archive:
            members = {member.name: member for member in archive.getmembers()}
            self.assertEqual(
                list(members),
                [
                    f"{prefix}/README.md",
                    f"{prefix}/third-party-sources.json",
                    f"{prefix}/python-image/busybox-1.37.0-r31/APKBUILD",
                    f"{prefix}/python-image/busybox-1.37.0-r31/busybox-1.37.0.tar.bz2",
                ],
            )
            self.assertEqual(
                archive.extractfile(
                    members[f"{prefix}/python-image/busybox-1.37.0-r31/APKBUILD"]
                ).read(),
                FILES["APKBUILD"],
            )
            readme = archive.extractfile(members[f"{prefix}/README.md"]).read()
            listing = archive.extractfile(members[f"{prefix}/third-party-sources.json"])
            self.assertEqual(json.load(listing), self.sources)
        self.assertIn(b"# Third-party sources for Vikram 0.2.0", readme)
        self.assertEqual(
            sorted(path.name for path in first.parent.iterdir()), [first.name]
        )

    def test_changed_or_oversized_sources_publish_nothing(self):
        url = "https://example.test/busybox-1.37.0.tar.bz2"
        for served, message in (
            (b"BZh8", "does not match its pinned digest"),
            (b"BZh9 and more", "larger than pinned"),
        ):
            with self.subTest(message=message):
                self.hosts.files[url] = served
                destination = self.root / message.split()[0]
                destination.mkdir()
                with self.assertRaisesRegex(SourceError, message):
                    third_party.fetch_sources(
                        self.hosts.client(self), self.sources, "0.2.0", destination
                    )
                self.assertEqual(list(destination.iterdir()), [])
        del self.hosts.files[url]
        with self.assertRaisesRegex(SourceError, "HTTP 404"):
            self.fetch("missing")

    def test_check_published_reports_moved_or_changed_files(self):
        session = self.hosts.client(self)
        self.assertEqual(
            third_party.check_published(session, self.sources),
            sum(len(data) for data in FILES.values()),
        )
        url = "https://example.test/APKBUILD"
        self.hosts.heads[url] = {"content-length": "3"}
        with self.assertRaisesRegex(SourceError, "changed size"):
            third_party.check_published(session, self.sources)
        self.hosts.heads[url] = {}  # Hosts may not report a size.
        third_party.check_published(session, self.sources)
        del self.hosts.heads[url], self.hosts.files[url]
        with self.assertRaisesRegex(SourceError, "not published"):
            third_party.check_published(session, self.sources)

    def test_temporary_host_failures_are_tried_again(self):
        recipe = "https://example.test/APKBUILD"
        tarball = "https://example.test/busybox-1.37.0.tar.bz2"
        session = self.hosts.client(self)
        self.hosts.failures[recipe] = [502]
        third_party.check_published(session, self.sources)
        self.assertEqual(self.hosts.requests.count(("HEAD", recipe)), 2)
        self.hosts.failures[tarball] = [503, httpx.ConnectError("reset"), 429]
        with tarfile.open(self.fetch("retried")) as archive:
            self.assertEqual(len(archive.getmembers()), 4)
        self.assertEqual(self.hosts.requests.count(("GET", tarball)), 4)
        self.hosts.requests.clear()
        self.hosts.failures[tarball] = [502] * third_party.ATTEMPTS
        with self.assertRaisesRegex(SourceError, "HTTP 502"):
            self.fetch("unavailable")
        self.assertEqual(
            self.hosts.requests.count(("GET", tarball)), third_party.ATTEMPTS
        )
        # A missing file is an answer, not a temporary failure.
        self.hosts.requests.clear()
        self.hosts.failures[recipe] = [404]
        with self.assertRaisesRegex(SourceError, "HTTP 404"):
            self.fetch("missing-recipe")
        self.assertEqual(self.hosts.requests, [("GET", recipe)])

    def test_release_assets_are_the_notices_and_the_checked_sources(self):
        archive = self.root / "Vikram-0.2.0-macos-arm64.zip"
        prefix = (
            f"{releases.ARCHIVE_ROOT}/Contents/Resources/Vikram.app/"
            f"{third_party.APP_NOTICES}/"
        )
        document = f"Vikram 0.2.0 includes\n<{third_party.sources_url('0.2.0')}>\n"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr(prefix + third_party.NOTICES_FILE, document)
            output.writestr(prefix + third_party.SOURCES_FILE, json.dumps(self.sources))
        work = self.root / "work"
        work.mkdir()
        session = self.hosts.client(self)
        assets = third_party.release_assets(archive, "0.2.0", work, session)
        self.assertEqual(
            [(path.name, kind) for path, kind in assets],
            [
                ("Vikram-0.2.0-THIRD-PARTY-NOTICES.md", "text/markdown; charset=utf-8"),
                ("Vikram-0.2.0-third-party-sources.tar", "application/x-tar"),
            ],
        )
        self.assertEqual(assets[0][0].read_text(), document)
        self.assertFalse(session.is_closed)

    def test_a_packed_sources_archive_is_checked_offline(self):
        tarball = self.fetch("packed")
        third_party.check_sources_archive(tarball, self.sources, "0.2.0")
        prefix = "Vikram-0.2.0-third-party-sources"
        recipe = f"{prefix}/python-image/busybox-1.37.0-r31/APKBUILD"

        def rewrite(name, change):
            """A copy of the archive with one member changed, added, or dropped."""
            copy = self.root / f"{name}.tar"
            with tarfile.open(tarball) as source, tarfile.open(copy, "w") as output:
                for member in source.getmembers():
                    data = source.extractfile(member).read()
                    for entry, content in change(member.name, data):
                        info = tarfile.TarInfo(entry)
                        info.size = len(content)
                        output.addfile(info, io.BytesIO(content))
            return copy

        cases = {
            "pinned digest": lambda n, d: [(n, b"x" * len(d) if n == recipe else d)],
            "unexpected": lambda n, d: [(n, d)]
            + ([(f"{prefix}/extra.sh", b"echo")] if n == recipe else []),
            "missing": lambda n, d: [] if n == recipe else [(n, d)],
            "match this release": lambda n, d: [
                (n, d.replace(b"0.2.0", b"0.9.9") if n.endswith("README.md") else d)
            ],
        }
        for message, change in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(SourceError, message):
                    third_party.check_sources_archive(
                        rewrite(message.split()[0], change), self.sources, "0.2.0"
                    )
        # The archive is for the version it names.
        with self.assertRaisesRegex(SourceError, "unexpected"):
            third_party.check_sources_archive(tarball, self.sources, "0.3.0")

    def test_release_assets_use_the_release_macs_sources_without_a_network(self):
        tarball = self.fetch("from-mac")
        archive = self.root / "Vikram-0.2.0-macos-arm64.zip"
        prefix = (
            f"{releases.ARCHIVE_ROOT}/Contents/Resources/Vikram.app/"
            f"{third_party.APP_NOTICES}/"
        )
        document = f"Vikram 0.2.0 includes\n<{third_party.sources_url('0.2.0')}>\n"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr(prefix + third_party.NOTICES_FILE, document)
            output.writestr(prefix + third_party.SOURCES_FILE, json.dumps(self.sources))
        work = self.root / "work-offline"
        work.mkdir()
        self.hosts.requests.clear()
        with patch.object(third_party, "client", side_effect=AssertionError("network")):
            assets = third_party.release_assets(
                archive, "0.2.0", work, sources_archive=tarball
            )
        self.assertEqual(self.hosts.requests, [])
        self.assertEqual(assets[1][0].read_bytes(), tarball.read_bytes())
        self.assertEqual(assets[1][0].parent, work)

    def test_downloads_carry_no_credentials_or_compression(self):
        with third_party.client() as session:
            self.assertNotIn("authorization", session.headers)
            self.assertEqual(session.headers["accept-encoding"], "identity")
            self.assertFalse(session.cookies)


class SourceListTests(unittest.TestCase):
    """The checked-in source list matches what the build bundles."""

    def setUp(self):
        self.sources = json.loads(third_party.SOURCES.read_text())

    def test_pins_the_bundled_image_and_the_locked_runtime(self):
        third_party.validate_sources(self.sources)
        self.assertEqual(self.sources["inputs"]["image"], IMAGE_MANIFEST)
        lock = tomllib.loads((third_party.ROOT / "uv.lock").read_text())
        [smol] = [p for p in lock["package"] if p["name"] == "smolmachines"]
        self.assertEqual(self.sources["inputs"]["smolmachines"], smol["version"])

    def test_every_copyleft_package_has_its_source(self):
        folders = {c["folder"] for c in self.sources["components"]}
        self.assertEqual(len(folders), len(self.sources["components"]))
        for group, prefix in (("guest", "vm-guest"), ("image", "python-image")):
            for item in self.sources[group]["packages"]:
                if third_party.COPYLEFT.search(item["license"]):
                    folder = f"{prefix}/{item['origin']}-{item['version']}"
                    self.assertIn(folder, folders, item["name"])
        self.assertTrue(any(f.startswith("vm-runtime/linux-") for f in folders))
        for component in self.sources["components"]:
            for item in component["files"]:
                self.assertRegex(
                    item["url"],
                    r"^https://(cdn\.kernel\.org|github\.com|gitlab\.alpinelinux\.org"
                    r"|distfiles\.alpinelinux\.org)/",
                )

    def test_every_license_in_use_has_its_text_or_is_named_without_one(self):
        texts = {path.stem for path in third_party.LICENSES.glob("*.txt")}
        used = third_party.referenced_licenses(self.sources)
        self.assertEqual(texts - used, set())
        self.assertEqual(used - texts, {"LGPL-1.0-only", "Public-Domain"})
        for path in third_party.LICENSES.glob("GPL-*.txt"):
            self.assertRegex(
                path.read_text(), re.compile("GNU GENERAL PUBLIC LICENSE", re.I)
            )
