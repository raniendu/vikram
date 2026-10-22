"""Fetch the digest-pinned Python image the sandbox VM boots, as a local archive.

The desktop build bundles the archive so code never needs the network. In a
checkout, run this once to enable the sandbox: `uv run python scripts/fetch_sandbox_image.py`.
"""

import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vikram.sandbox import (  # noqa: E402
    IMAGE_FILE,
    IMAGE_MANIFEST,
    IMAGE_REPOSITORY,
    IMAGE_TAG,
    REGISTRY_MANIFEST,
    verify_archive,
)

REGISTRY = "https://registry-1.docker.io"
MANIFEST_TYPES = ", ".join(
    [
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


def _verified(data: bytes, digest: str) -> bytes:
    if f"sha256:{hashlib.sha256(data).hexdigest()}" != digest:
        raise SystemExit(f"Digest mismatch for {digest}; refusing to use it.")
    return data


def fetch(destination: Path) -> Path:
    """Download the pinned manifest's config and layers, verifying every digest."""
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        token = client.get(
            "https://auth.docker.io/token",
            params={
                "service": "registry.docker.io",
                "scope": f"repository:{IMAGE_REPOSITORY}:pull",
            },
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        base = f"{REGISTRY}/v2/{IMAGE_REPOSITORY}"
        registry = _verified(
            client.get(
                f"{base}/manifests/{IMAGE_MANIFEST}",
                headers={**headers, "Accept": MANIFEST_TYPES},
            ).content,
            IMAGE_MANIFEST,
        )
        manifest = json.loads(registry)
        blobs = [manifest["config"], *manifest["layers"]]
        files = {}
        for blob in blobs:
            response = client.get(f"{base}/blobs/{blob['digest']}", headers=headers)
            response.raise_for_status()
            files[blob["digest"].replace(":", "-")] = _verified(
                response.content, blob["digest"]
            )
    entry = {
        "Config": manifest["config"]["digest"].replace(":", "-"),
        "Layers": [layer["digest"].replace(":", "-") for layer in manifest["layers"]],
        "RepoTags": [f"{IMAGE_REPOSITORY.removeprefix('library/')}:{IMAGE_TAG}"],
    }
    # The registry manifest lets the runtime re-check the archive against the pin.
    files = {
        "manifest.json": json.dumps([entry]).encode(),
        REGISTRY_MANIFEST: registry,
        **files,
    }
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / IMAGE_FILE
    temporary = path.with_suffix(".tmp")
    # A deterministic archive: fixed order, owner, and timestamps.
    with tarfile.open(temporary, "w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            archive.addfile(info, io.BytesIO(data))
    if not verify_archive(temporary):
        temporary.unlink(missing_ok=True)
        raise SystemExit("The fetched image archive failed verification.")
    temporary.replace(path)
    return path


def ensure(destination: Path = ROOT / "build/sandbox") -> Path:
    path = destination / IMAGE_FILE
    if path.is_file() and verify_archive(path):
        return path
    return fetch(destination)


if __name__ == "__main__":
    print(ensure())
