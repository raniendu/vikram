"""Bounded, anonymous updates from the project's stable GitHub Releases."""

import argparse
import contextlib
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlsplit

import httpx

from vikram.installation import (
    MAX_EXPANDED,
    REPOSITORY,
    InstallError,
    bundle_version,
    check_platform,
    cli_version,
    ensure_space,
    install_bundle,
    install_lock,
    installed_version,
    prune_versions,
    publish_app,
    runtime_layout,
    version_tuple,
)

LATEST_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
MAX_DOWNLOAD = 256 * 1024**2
MAX_METADATA = 1024**2
ASSET_HOSTS = {
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
}
ARCHIVE_ROOT = "Install Vikram.app"


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    size: int
    sha256: str


def make_client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(30, connect=10),
        follow_redirects=False,
        trust_env=False,
        headers={
            "User-Agent": f"Vikram/{cli_version()}",
            "Accept-Encoding": "identity",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )


def _identity(response: httpx.Response) -> None:
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise InstallError("GitHub returned an unsupported compressed response.")


def _status(response: httpx.Response) -> None:
    if response.status_code in {403, 429}:
        raise InstallError(
            "GitHub refused or rate-limited this request. Try again later."
        )
    if response.status_code != 200:
        raise InstallError(
            f"GitHub returned HTTP {response.status_code}. The installed version is unchanged."
        )


def parse_release(data: dict) -> Release:
    try:
        if data.get("draft") is not False or data.get("prerelease") is not False:
            raise ValueError()
        tag = data["tag_name"]
        if not isinstance(tag, str) or not tag.startswith("v"):
            raise ValueError()
        version = tag[1:]
        version_tuple(version)
        name = f"Vikram-{version}-macos-arm64.zip"
        assets = [asset for asset in data["assets"] if asset["name"] == name]
        if len(assets) != 1:
            raise ValueError()
        asset = assets[0]
        url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"
        digest = asset.get("digest")
        if (
            asset["browser_download_url"] != url
            or asset["state"] != "uploaded"
            or type(asset["size"]) is not int
            or not 0 < asset["size"] <= MAX_DOWNLOAD
            or not isinstance(digest, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
        ):
            raise ValueError()
        return Release(version, url, asset["size"], digest[7:])
    except (KeyError, TypeError, ValueError, AttributeError):
        raise InstallError(
            "The latest release has no valid, checksummed Apple Silicon installer. The installed version is unchanged."
        ) from None


def latest_release(client: httpx.Client) -> Release | None:
    with client.stream(
        "GET", LATEST_URL, headers={"Accept": "application/vnd.github+json"}
    ) as response:
        if response.status_code == 404:
            return None
        _status(response)
        _identity(response)
        data = bytearray()
        deadline = time.monotonic() + 45
        for chunk in response.iter_bytes():
            if len(data) + len(chunk) > MAX_METADATA or time.monotonic() > deadline:
                raise InstallError(
                    "GitHub release metadata exceeded the size or time limit."
                )
            data.extend(chunk)
    try:
        return parse_release(json.loads(data))
    except (ValueError, UnicodeError):
        raise InstallError("GitHub returned invalid release metadata.") from None


def _asset_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in ASSET_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.fragment
        ):
            raise ValueError()
    except ValueError:
        raise InstallError(
            "The release redirected to an untrusted download location."
        ) from None


def download_release(client: httpx.Client, release: Release, destination: Path) -> Path:
    url, size = release.url, 0
    digest = hashlib.sha256()
    deadline = time.monotonic() + 300
    complete = False
    created = False
    try:
        for _ in range(4):
            _asset_url(url)
            with client.stream("GET", url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise InstallError(
                            "GitHub returned a redirect without a location."
                        )
                    url = urljoin(url, location)
                    continue
                _status(response)
                _identity(response)
                with destination.open("xb") as stream:
                    created = True
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > release.size or time.monotonic() > deadline:
                            raise InstallError(
                                "The release download exceeded the size or time limit."
                            )
                        digest.update(chunk)
                        stream.write(chunk)
                if size != release.size or digest.hexdigest() != release.sha256:
                    raise InstallError(
                        "Release checksum verification failed. Nothing was installed."
                    )
                complete = True
                return destination
        raise InstallError("GitHub returned too many download redirects.")
    finally:
        if created and not complete:
            destination.unlink(missing_ok=True)


def extract_release(archive: Path, destination: Path) -> Path:
    """Validate every name and expansion bound before creating extracted files."""
    try:
        with zipfile.ZipFile(archive) as source:
            entries = source.infolist()
            if len(entries) > 10000 or sum(i.file_size for i in entries) > MAX_EXPANDED:
                raise InstallError(
                    "The installer archive exceeds the extraction limit."
                )
            names, spellings = {}, {}
            for item in entries:
                name = item.filename.rstrip("/")
                path = PurePosixPath(name)
                kind = stat.S_IFMT(item.external_attr >> 16)
                key = unicodedata.normalize("NFC", name).casefold()
                if (
                    not name
                    or len(name) > 1024
                    or "\\" in name
                    or "\x00" in item.orig_filename
                    or name != path.as_posix()
                    or path.is_absolute()
                    or ".." in path.parts
                    or path.parts[0] != ARCHIVE_ROOT
                    or kind not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or (kind == stat.S_IFDIR and not item.is_dir())
                    or (kind == stat.S_IFREG and item.is_dir())
                    or item.flag_bits & 1
                    or key in names
                    or item.compress_type
                    not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                ):
                    raise InstallError(
                        "The installer archive contains an unsafe path or file type."
                    )
                for component in [path, *path.parents]:
                    spelling = str(component)
                    canonical = unicodedata.normalize("NFC", spelling).casefold()
                    if canonical in spellings and spellings[canonical] != spelling:
                        raise InstallError(
                            "The installer archive contains conflicting path spellings."
                        )
                    spellings[canonical] = spelling
                names[key] = item.is_dir()
            for name in names:
                for parent in PurePosixPath(name).parents:
                    if names.get(str(parent)) is False:
                        raise InstallError(
                            "The installer archive contains conflicting paths."
                        )
            destination.mkdir(mode=0o700, parents=True, exist_ok=False)
            total = 0
            for item in entries:
                output = destination / item.filename
                if item.is_dir():
                    output.mkdir(mode=0o755, parents=True, exist_ok=True)
                    continue
                output.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                with source.open(item) as input_file, output.open("xb") as output_file:
                    size = 0
                    while chunk := input_file.read(64 * 1024):
                        size += len(chunk)
                        total += len(chunk)
                        if size > item.file_size or total > MAX_EXPANDED:
                            raise InstallError(
                                "The installer archive exceeds the extraction limit."
                            )
                        output_file.write(chunk)
                output.chmod(0o755 if item.external_attr >> 16 & 0o111 else 0o644)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError, ValueError):
        raise InstallError(
            "The installer archive could not be safely extracted."
        ) from None
    app = destination / ARCHIVE_ROOT / "Contents/Resources/Vikram.app"
    if not app.is_dir():
        raise InstallError("The release archive does not contain the Vikram app.")
    return app


def install_release(
    client: httpx.Client, layout, release: Release, say=print
) -> str | None:
    """Download, verify, and install `release` for the CLI and Mac app.

    Returns the installed version, or `None` if another install already
    brought this or a newer version while the metadata request was in flight.
    """
    with install_lock(layout):
        current = installed_version(layout)
        if current and version_tuple(release.version) <= version_tuple(current):
            publish_app(layout)
            return None
        ensure_space(layout.root, release.size + 2 * MAX_EXPANDED)
        with tempfile.TemporaryDirectory(prefix="download-", dir=layout.root) as work:
            say(f"Downloading and verifying Vikram {release.version}…")
            archive = download_release(client, release, Path(work) / "release.zip")
            ensure_space(layout.root, 2 * MAX_EXPANDED)
            app = extract_release(archive, Path(work) / "unpacked")
            if bundle_version(app) != release.version:
                raise InstallError(
                    "The downloaded app version does not match the release."
                )
            install_bundle(app, layout, _locked=True)
        # Raise now if the app could not be updated, rather than claim it was.
        publish_app(layout)
    return release.version


def repair_app(layout) -> None:
    """Bring the app in ~/Applications to the installed version, if behind,
    and remove old versions that an earlier update had to keep."""
    with install_lock(layout):
        publish_app(layout)
        # Never fail a launch or `vikram update` over this.
        with contextlib.suppress(InstallError, OSError):
            prune_versions(layout)


def update_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="vikram update",
        description="Update both the CLI and Mac app from the latest stable GitHub release.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report availability without downloading or installing",
    )
    args = parser.parse_args(argv)
    try:
        check_platform()
        layout = runtime_layout()
        installed = installed_version(layout)
        current = installed or cli_version()
        if not args.check and installed is None:
            raise InstallError(
                "Install both components with Install Vikram.app first. Use `vikram update --check` to check published releases."
            )
        print(f"Checking for updates (current {current})…")
        with make_client() as client:
            release = latest_release(client)
            if release is None:
                print(
                    "No stable Vikram releases have been published yet. Nothing changed."
                )
            elif version_tuple(release.version) <= version_tuple(current):
                if installed and not args.check:
                    repair_app(layout)
                print(
                    f"Vikram {current} is up to date; latest published release is {release.version}."
                )
            elif args.check:
                print(
                    f"Vikram {release.version} is available for both the CLI and Mac app. Run `vikram update` after installing with Install Vikram.app."
                )
            elif install_release(client, layout, release) is None:
                print(f"Vikram {installed_version(layout)} is already up to date.")
            else:
                print(
                    f"Updated CLI and Mac app to Vikram {release.version}. An open Mac app offers to restart into it; running chats were not interrupted."
                )
        return 0
    except (InstallError, httpx.HTTPError, OSError) as error:
        message = (
            str(error)
            if isinstance(error, InstallError)
            else "Could not download or install the update. Check your connection, write permissions and disk space. The current version is unchanged."
        )
        sys.stderr.write(message + "\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("\nUpdate cancelled.\n")
        return 130
