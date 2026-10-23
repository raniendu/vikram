"""Publish a built installer archive as the latest stable GitHub Release.

The release workflow runs this on a GitHub-hosted runner after the release Mac
built and checked the archive. It creates a draft release for the pushed tag,
uploads the archive and its checksum file, checks GitHub's stored copy against
the build, and only then publishes the draft as the latest release. Finally it
reads the latest release back with the updater's own parser, which requires
GitHub's SHA-256 digest. A published release is never changed; a draft left by a
failed attempt is reused.

    uv run python scripts/publish_release.py --dry-run --tag v0.2.0 ARCHIVE
"""

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vikram.installation import REPOSITORY, InstallError, version_tuple  # noqa: E402
from vikram.releases import Release, parse_release  # noqa: E402

API = f"https://api.github.com/repos/{REPOSITORY}"
# The same REST API version the updater requests.
API_VERSION = "2026-03-10"


class PublishError(Exception):
    """A failure that leaves every published release unchanged."""


def asset_name(version: str) -> str:
    return f"Vikram-{version}-macos-arm64.zip"


def check_archive(tag: str, archive: Path, expected: str | None = None) -> Release:
    """Return the release the updater would accept for this tag and archive."""
    version = tag.removeprefix("v")
    try:
        if version == tag:
            raise InstallError()
        version_tuple(version)
    except InstallError:
        raise PublishError(f"{tag} is not a vMAJOR.MINOR.PATCH tag.") from None
    if archive.name != asset_name(version):
        raise PublishError(f"{tag} needs {asset_name(version)}, not {archive.name}.")
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    checksum = archive.with_name(f"{archive.name}.sha256")
    if not checksum.is_file() or checksum.read_text() != f"{digest}  {archive.name}\n":
        raise PublishError(f"{checksum.name} does not match the archive.")
    if expected is not None and expected != digest:
        raise PublishError("This archive is not the one the release Mac built.")
    size = archive.stat().st_size
    url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{archive.name}"
    # The asset metadata GitHub reports after the upload, read as the updater would.
    asset = {
        "name": archive.name,
        "size": size,
        "state": "uploaded",
        "digest": f"sha256:{digest}",
        "browser_download_url": url,
    }
    try:
        return parse_release(
            {"tag_name": tag, "draft": False, "prerelease": False, "assets": [asset]}
        )
    except InstallError:
        raise PublishError(
            f"`vikram update` would refuse this {size}-byte archive."
        ) from None


def release_notes(release: Release) -> str:
    name = asset_name(release.version)
    return f"""\
Vikram {release.version} for Apple Silicon Macs with macOS 13 or newer: the desktop \
app and CLI in one installer. Vikram uses your local Ollama models.

- Update an installed copy: `vikram update`
- Install for the first time: download `{name}`, unzip it, and open \
**Install Vikram.app**.

This build is ad-hoc signed and not notarized. `vikram update` verifies and \
installs it, but macOS may refuse to open a copy downloaded in a web browser. See \
[Installing and releasing Vikram](https://github.com/{REPOSITORY}/blob/main/docs/releases.md).

SHA-256 of `{name}`: `{release.sha256}`
"""


class GitHub:
    """The few REST calls publishing needs, failing on any unexpected status."""

    def __init__(self, client: httpx.Client):
        self.client = client

    def call(self, method: str, url: str, **options) -> httpx.Response:
        response = self.client.request(method, url, **options)
        if not response.is_success:
            try:
                body = response.json()
                message = body.get("message", "") if isinstance(body, dict) else ""
            except ValueError:
                message = ""
            raise PublishError(
                f"GitHub returned HTTP {response.status_code} for {method} "
                f"{url.split('?')[0]}. {message}".strip()
            )
        return response

    def tag_commit(self, tag: str) -> str:
        target = self.call("GET", f"{API}/git/ref/tags/{tag}").json()["object"]
        # An annotated tag points to a tag object, which points to the commit.
        for _ in range(5):
            if target["type"] == "commit":
                return target["sha"]
            if target["type"] != "tag":
                break
            target = self.call("GET", f"{API}/git/tags/{target['sha']}").json()[
                "object"
            ]
        raise PublishError(f"{tag} does not point to a commit.")

    def releases_for(self, tag: str) -> list[dict]:
        found, page = [], 1
        while True:
            batch = self.call(
                "GET", f"{API}/releases", params={"per_page": 100, "page": page}
            ).json()
            found += [release for release in batch if release["tag_name"] == tag]
            if len(batch) < 100:
                return found
            page += 1

    def create_draft(self, tag: str, commit: str, release: Release) -> dict:
        return self.call(
            "POST",
            f"{API}/releases",
            json={
                "tag_name": tag,
                "target_commitish": commit,
                "name": f"Vikram {release.version}",
                "body": release_notes(release),
                "draft": True,
                "prerelease": False,
                "generate_release_notes": True,
            },
        ).json()

    def delete_asset(self, asset_id: int) -> None:
        self.call("DELETE", f"{API}/releases/assets/{asset_id}")

    def upload(self, draft: dict, path: Path, content_type: str) -> dict:
        return self.call(
            "POST",
            draft["upload_url"].split("{", 1)[0],
            params={"name": path.name},
            content=path.read_bytes(),
            headers={"Content-Type": content_type},
            timeout=httpx.Timeout(600, connect=10),
        ).json()

    def asset_sha256(self, asset_id: int) -> str:
        """Hash GitHub's stored copy of an asset, which works for drafts too."""
        digest = hashlib.sha256()
        with self.client.stream(
            "GET",
            f"{API}/releases/assets/{asset_id}",
            headers={
                "Accept": "application/octet-stream",
                "Accept-Encoding": "identity",
            },
            # httpx drops the token when GitHub redirects to its storage host.
            follow_redirects=True,
            timeout=httpx.Timeout(600, connect=10),
        ) as response:
            if not response.is_success:
                raise PublishError(
                    f"GitHub returned HTTP {response.status_code} for the uploaded "
                    "archive."
                )
            for chunk in response.iter_bytes():
                digest.update(chunk)
        return digest.hexdigest()

    def release(self, release_id: int) -> dict:
        return self.call("GET", f"{API}/releases/{release_id}").json()

    def publish(self, release_id: int) -> dict:
        return self.call(
            "PATCH",
            f"{API}/releases/{release_id}",
            json={"draft": False, "prerelease": False, "make_latest": "true"},
        ).json()

    def latest(self) -> dict:
        return self.call("GET", f"{API}/releases/latest").json()


def publish(
    github: GitHub,
    tag: str,
    commit: str,
    archive: Path,
    release: Release,
    *,
    attempts: int = 10,
    delay: float = 3.0,
) -> str:
    """Publish the checked archive for tag and return the release page URL."""
    if github.tag_commit(tag) != commit:
        raise PublishError(f"{tag} no longer points to the built commit {commit}.")
    existing = github.releases_for(tag)
    if any(not candidate["draft"] for candidate in existing):
        raise PublishError(
            f"{tag} is already published, and published releases are never "
            "replaced. Bump the version and tag the new one."
        )
    if len(existing) > 1:
        raise PublishError(f"{tag} has {len(existing)} drafts. Delete all but one.")
    if existing:
        draft = existing[0]
        print(f"Reusing the draft release for {tag} from an earlier attempt.")
    else:
        draft = github.create_draft(tag, commit, release)
        print(f"Created a draft release for {tag}.")
    checksum = archive.with_name(f"{archive.name}.sha256")
    for path, content_type in ((archive, "application/zip"), (checksum, "text/plain")):
        for asset in draft["assets"]:
            if asset["name"] == path.name:
                github.delete_asset(asset["id"])
        github.upload(draft, path, content_type)
        print(f"Uploaded {path.name}.")
    # Nobody can see the draft yet: compare GitHub's copy with the build first.
    uploaded = [
        asset
        for asset in github.release(draft["id"])["assets"]
        if asset["name"] == archive.name
    ]
    if (
        len(uploaded) != 1
        or uploaded[0]["state"] != "uploaded"
        or uploaded[0]["size"] != release.size
        or uploaded[0].get("digest") not in (None, f"sha256:{release.sha256}")
        or github.asset_sha256(uploaded[0]["id"]) != release.sha256
    ):
        raise PublishError(
            "GitHub's copy of the archive does not match the build. The release "
            "is still an unpublished draft."
        )
    page = github.publish(draft["id"])["html_url"]
    print(f"Published {tag} as the latest release.")
    for attempt in range(attempts):
        if attempt:
            time.sleep(delay)
        try:
            if parse_release(github.latest()) == release:
                return page
        except (InstallError, PublishError):
            pass
    raise PublishError(
        f"{tag} is published, but GitHub does not yet report it as the latest "
        f"release `vikram update` would install. Check {page}."
    )


def _outputs(**values: str) -> None:
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.writelines(f"{key}={value}\n" for key, value in values.items())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("archive", type=Path, help="Vikram-VERSION-macos-arm64.zip")
    parser.add_argument("--tag", required=True, help="the vMAJOR.MINOR.PATCH tag")
    parser.add_argument("--commit", help="the commit the release Mac built")
    parser.add_argument("--sha256", help="the archive digest the release Mac built")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="check the archive and describe the release without contacting GitHub",
    )
    args = parser.parse_args()
    try:
        release = check_archive(args.tag, args.archive, args.sha256)
        if args.dry_run:
            print(
                f"{args.archive.name} ({release.size} bytes, SHA-256 "
                f"{release.sha256}) passes the updater's checks for {args.tag}. "
                "Dry run: nothing was published."
            )
            _outputs(version=release.version, sha256=release.sha256)
            return
        token = os.environ.get("GITHUB_TOKEN")
        if not token or not args.commit:
            raise PublishError("Publishing needs --commit and GITHUB_TOKEN.")
        with httpx.Client(
            timeout=httpx.Timeout(60, connect=10),
            follow_redirects=False,
            trust_env=False,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "vikram-release",
                "X-GitHub-Api-Version": API_VERSION,
            },
        ) as client:
            page = publish(GitHub(client), args.tag, args.commit, args.archive, release)
    except (PublishError, OSError, httpx.HTTPError) as error:
        raise SystemExit(f"Release stopped: {error}") from None
    print(page)
    _outputs(url=page)


if __name__ == "__main__":
    main()
