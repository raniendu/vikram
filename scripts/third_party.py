"""Third-party notices and sources for the Python sandbox's copyleft components.

The sandbox's VM runtime (smolmachines: libkrunfw with its Linux kernel, and an
Alpine guest filesystem) and its pinned Python image include GPL, LGPL and MPL
software. `scripts/third-party/sources.json` pins the exact source of each such
component, file by file, and the license texts their packages name are kept
beside it. The desktop build writes the notices into Vikram.app; publishing
downloads the pinned sources, checks every digest, and attaches them to the
release.

    uv run python scripts/third_party.py refresh
    uv run python scripts/third_party.py notices
    uv run python scripts/third_party.py sources [--check] ARCHIVE
"""

import argparse
import contextlib
import email.parser
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import zipfile
from collections.abc import Callable
from importlib.metadata import version as package_version
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vikram.installation import REPOSITORY, InstallError, ensure_space  # noqa: E402
from vikram.releases import ARCHIVE_ROOT  # noqa: E402
from vikram.sandbox import IMAGE_MANIFEST, IMAGE_REPOSITORY, IMAGE_TAG  # noqa: E402

DATA = ROOT / "scripts/third-party"
SOURCES = DATA / "sources.json"
LICENSES = DATA / "licenses"
# The desktop build writes the notices here, and Tauri bundles the folder into
# Vikram.app (`bundle.resources` in tauri.conf.json).
NOTICES = ROOT / "apps/desktop/src-tauri/notices"
APP_NOTICES = "Contents/Resources/notices"
NOTICES_FILE = "THIRD-PARTY-NOTICES.md"
SOURCES_FILE = "third-party-sources.json"
FORMAT = 1
ALPINE = "https://gitlab.alpinelinux.org/alpine/aports"
DISTFILES = "https://distfiles.alpinelinux.org/distfiles"
KERNEL = "https://cdn.kernel.org/pub/linux/kernel"
LIBKRUNFW = "smol-machines/libkrunfw"
SPDX = "https://raw.githubusercontent.com/spdx/license-list-data/v3.29.0/text"
# A host's temporary failures, which a request survives by trying again.
TRANSIENT = {429, 500, 502, 503, 504}
ATTEMPTS, DELAY = 4, 2.0
# Licenses whose terms reach the source code: GPL, LGPL, AGPL and MPL.
COPYLEFT = re.compile(r"(?<![A-Za-z])(?:A?GPL|LGPL|MPL)-")
PYTHON_FILES = re.compile(
    r"usr/local/lib/python3\.\d+/"
    r"(LICENSE\.txt|site-packages/[^/]+\.dist-info/(?:METADATA|licenses/.+))"
)


class SourceError(Exception):
    """A notices or sources problem, safe to show. Nothing was published."""


def sources_asset(version: str) -> str:
    return f"Vikram-{version}-third-party-sources.tar"


def notices_asset(version: str) -> str:
    return f"Vikram-{version}-THIRD-PARTY-NOTICES.md"


def sources_url(version: str) -> str:
    return (
        f"https://github.com/{REPOSITORY}/releases/download/v{version}/"
        f"{sources_asset(version)}"
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def license_ids(expression: str) -> set[str]:
    """The license identifiers in an SPDX-style expression."""
    return {
        token
        for token in re.findall(r"[^\s()]+", expression)
        if token not in {"AND", "OR", "WITH"}
    }


def apk_packages(database: str) -> list[dict[str, str]]:
    """The packages in an apk database: name, version, license, origin and commit."""
    # `c` is the aports commit the package was built from.
    fields = {"P": "name", "V": "version", "L": "license", "o": "origin", "c": "commit"}
    packages = []
    for block in database.split("\n\n"):
        record: dict[str, str] = {}
        for line in block.splitlines():
            key, separator, value = line.partition(":")
            if separator and key in fields:
                record.setdefault(fields[key], value)
        # Virtual packages, such as the image's `.python-rundeps`, have no origin.
        if record.get("name") and record.get("origin"):
            packages.append({field: record.get(field, "") for field in fields.values()})
    return sorted(packages, key=lambda package: package["name"])


def apkbuild_version(recipe: str) -> str:
    values = dict(re.findall(r"^(pkgver|pkgrel)=[\"']?([^\"'\s]+)", recipe, re.M))
    return f"{values.get('pkgver')}-r{values.get('pkgrel')}"


def apkbuild_checksums(recipe: str) -> list[tuple[str, str]]:
    """The (SHA-512, file name) pairs an APKBUILD pins for every source file."""
    match = re.search(r"^sha512sums=([\"'])(.*?)\1", recipe, re.M | re.S)
    if match is None:
        return []
    return re.findall(r"^\s*([0-9a-f]{128})\s+(\S+)\s*$", match.group(2), re.M)


def image_files(
    path: Path, wanted: Callable[[str], bool]
) -> tuple[dict, dict[str, bytes]]:
    """An image archive's config, and the wanted files its layers end up with."""
    with tarfile.open(path) as outer:
        blobs = {
            member.name: outer.extractfile(member).read()
            for member in outer.getmembers()
            if member.isfile()
        }
    [entry] = json.loads(blobs["manifest.json"])
    files: dict[str, bytes] = {}
    for layer in entry["Layers"]:
        with tarfile.open(fileobj=io.BytesIO(blobs[layer])) as archive:
            for member in archive:
                name = member.name.removeprefix("./")
                folder, _, base = name.rpartition("/")
                if base.startswith(".wh."):
                    # A whiteout hides a lower layer's file, or a folder's contents.
                    gone = folder if base == ".wh..wh..opq" else f"{folder}/{base[4:]}"
                    gone = gone.strip("/")
                    files = {
                        key: value
                        for key, value in files.items()
                        if key != gone and not key.startswith(f"{gone}/")
                    }
                elif member.isfile() and wanted(name):
                    files[name] = archive.extractfile(member).read()
    return json.loads(blobs[entry["Config"]]), files


def _tar_bytes(path: Path) -> bytes:
    if path.suffix != ".zst":
        return path.read_bytes()
    try:
        from compression import zstd  # Python 3.14 and later.
    except ImportError:
        tool = shutil.which("zstd")
        if tool is None:
            raise SourceError(
                "Reading the VM's guest filesystem needs Python 3.14 or the zstd "
                "command (`brew install zstd`)."
            ) from None
        return subprocess.run(
            [tool, "-dcq", str(path)], check=True, capture_output=True
        ).stdout
    return zstd.decompress(path.read_bytes())


def guest_filesystem(smol: Path) -> tuple[str, list[dict[str, str]]]:
    """The Alpine release and packages in smolmachines' guest filesystem."""
    path = next(
        (
            candidate
            for candidate in (smol / "agent-rootfs.tar.zst", smol / "agent-rootfs.tar")
            if candidate.is_file()
        ),
        None,
    )
    if path is None:
        raise SourceError("smolmachines has no guest filesystem to describe.")
    found = {}
    with tarfile.open(fileobj=io.BytesIO(_tar_bytes(path))) as archive:
        for member in archive:
            name = member.name.removeprefix("./")
            if name in ("etc/alpine-release", "lib/apk/db/installed"):
                if member.isfile():
                    found[name] = archive.extractfile(member).read().decode()
    if len(found) != 2:
        raise SourceError("smolmachines' guest filesystem has no Alpine package list.")
    return found["etc/alpine-release"].strip(), apk_packages(
        found["lib/apk/db/installed"]
    )


def smol_folder() -> Path:
    import smol

    return Path(smol.__file__).parent


def smol_pins(smol: Path) -> dict[str, str]:
    """Digests of the smolmachines files that decide its copyleft sources."""
    paths = {
        smol / "THIRD_PARTY_NOTICES.md",
        *smol.glob("libkrunfw*.dylib"),
        *smol.glob("agent-rootfs.tar*"),
    }
    return {path.name: _file_sha256(path) for path in sorted(paths) if path.is_file()}


def inputs(smol: Path) -> dict:
    """What a source list describes: the pinned image and the VM runtime's files."""
    return {
        "image": IMAGE_MANIFEST,
        "smolmachines": package_version("smolmachines"),
        "smol": smol_pins(smol),
    }


def check_inputs(sources: dict, smol: Path) -> None:
    if sources.get("format") != FORMAT or sources.get("inputs") != inputs(smol):
        raise SourceError(
            "The sandbox image or VM runtime changed since "
            "scripts/third-party/sources.json was made. Run `uv run python "
            "scripts/third_party.py refresh` and commit the result."
        )


def client() -> httpx.Client:
    """An anonymous client for third-party hosts: no credentials, no compression."""
    return httpx.Client(
        timeout=httpx.Timeout(120, connect=15),
        follow_redirects=True,
        headers={"User-Agent": "vikram-release", "Accept-Encoding": "identity"},
        transport=httpx.HTTPTransport(retries=3),
    )


def _send(session: httpx.Client, method: str, url: str) -> httpx.Response:
    """A request, tried again after a pause while the host fails temporarily."""
    for attempt in range(1, ATTEMPTS):
        try:
            response = session.request(method, url)
            if response.status_code not in TRANSIENT:
                return response
        except httpx.TransportError:
            pass
        time.sleep(DELAY * attempt)
    return session.request(method, url)


def _fetch(session: httpx.Client, url: str) -> httpx.Response:
    response = _send(session, "GET", url)
    if response.status_code != 200:
        raise SourceError(f"{url} returned HTTP {response.status_code}.")
    return response


def _published_size(session: httpx.Client, url: str) -> int | None:
    """The size a host reports for a file, or `None` when it does not say."""
    response = _send(session, "HEAD", url)
    if response.status_code != 200:
        raise SourceError(f"{url} is not published (HTTP {response.status_code}).")
    length = response.headers.get("content-length")
    return int(length) if length is not None else None


def runtime_components(session: httpx.Client, smol: Path) -> list[dict]:
    """The Linux kernel in libkrunfw, and libkrunfw's build files and patches."""
    notices = (smol / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    pinned = re.search(rf"github\.com/{LIBKRUNFW}/tree/([0-9a-f]{{40}})", notices)
    if pinned is None:
        raise SourceError("smolmachines' notices no longer name libkrunfw's source.")
    commit = pinned.group(1)
    makefile = _fetch(
        session, f"https://raw.githubusercontent.com/{LIBKRUNFW}/{commit}/Makefile"
    ).text
    kernel = re.search(r"^KERNEL_VERSION\s*=\s*linux-((\d+)\.\S+)\s*$", makefile, re.M)
    library = re.search(r"^FULL_VERSION\s*=\s*(\S+)\s*$", makefile, re.M)
    if kernel is None or library is None:
        raise SourceError(f"libkrunfw's Makefile at {commit} names no kernel release.")
    release, version = kernel.group(1), library.group(1)
    folder = f"{KERNEL}/v{kernel.group(2)}.x"
    tarball = f"linux-{release}.tar.xz"
    sums = _fetch(session, f"{folder}/sha256sums.asc").text
    digest = re.search(rf"^([0-9a-f]{{64}})\s+{re.escape(tarball)}\s*$", sums, re.M)
    size = _published_size(session, f"{folder}/{tarball}")
    if digest is None or size is None:
        raise SourceError(f"kernel.org does not list {tarball}.")
    archive = f"https://github.com/{LIBKRUNFW}/archive/{commit}.tar.gz"
    content = _fetch(session, archive).content
    return [
        {
            "folder": f"vm-runtime/linux-{release}",
            "name": "Linux",
            "version": release,
            "licenses": ["GPL-2.0-only"],
            "for": f"the kernel in libkrunfw {version}",
            "files": [
                {
                    "name": tarball,
                    "url": f"{folder}/{tarball}",
                    "size": size,
                    "sha256": digest.group(1),
                }
            ],
        },
        {
            "folder": f"vm-runtime/libkrunfw-{version}",
            "name": "libkrunfw",
            "version": version,
            "revision": commit,
            "licenses": ["GPL-2.0-only", "LGPL-2.1-only"],
            "for": "the VM runtime's kernel library, and the kernel's configuration "
            "and patches",
            "files": [
                {
                    "name": f"libkrunfw-{commit}.tar.gz",
                    "url": archive,
                    "size": len(content),
                    "sha256": _sha256(content),
                }
            ],
        },
    ]


def alpine_components(
    session: httpx.Client, packages: list[dict], release: str, folder: str
) -> list[dict]:
    """The aports recipe, patches and upstream files of each copyleft origin."""
    branch = "v" + ".".join(release.split(".")[:2])
    origins: dict[str, list[dict]] = {}
    for package in packages:
        if COPYLEFT.search(package["license"]):
            origins.setdefault(package["origin"], []).append(package)
    components = []
    for origin, members in sorted(origins.items()):
        builds = {(package["version"], package["commit"]) for package in members}
        version, commit = builds.pop() if len(builds) == 1 else ("", "")
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise SourceError(f"Alpine {release}: cannot tell which {origin} is built.")
        for repository in ("main", "community"):
            base = f"{ALPINE}/-/raw/{commit}/{repository}/{origin}"
            response = _send(session, "GET", f"{base}/APKBUILD")
            if response.status_code == 200:
                break
        else:
            raise SourceError(
                f"Alpine {release}: no APKBUILD for {origin} at {commit}."
            )
        recipe = response.content
        if apkbuild_version(recipe.decode()) != version:
            raise SourceError(
                f"Alpine {release}: the {origin} APKBUILD at {commit[:12]} is not "
                f"version {version}."
            )
        files = [
            {
                "name": "APKBUILD",
                "url": f"{base}/APKBUILD",
                "size": len(recipe),
                "sha256": _sha256(recipe),
            }
        ]
        for digest, name in apkbuild_checksums(recipe.decode()):
            # Patches and helper files live beside the recipe; Alpine's mirror
            # keeps every upstream file a release branch was built from.
            local = _send(session, "GET", f"{base}/{name}")
            if (
                local.status_code == 200
                and hashlib.sha512(local.content).hexdigest() == digest
            ):
                url, size = f"{base}/{name}", len(local.content)
            else:
                url = f"{DISTFILES}/{branch}/{name}"
                size = _published_size(session, url)
                if size is None:
                    raise SourceError(f"{url} does not report its size.")
            files.append({"name": name, "url": url, "size": size, "sha512": digest})
        components.append(
            {
                "folder": f"{folder}/{origin}-{version}",
                "name": origin,
                "version": version,
                "revision": commit,
                "aports": f"{repository}/{origin}",
                "licenses": sorted(
                    {i for package in members for i in license_ids(package["license"])}
                ),
                "for": ", ".join(package["name"] for package in members),
                "files": files,
            }
        )
    return components


def referenced_licenses(sources: dict) -> set[str]:
    packages = [*sources["image"]["packages"], *sources["guest"]["packages"]]
    return {i for package in packages for i in license_ids(package["license"])} | {
        i for component in sources["components"] for i in component["licenses"]
    }


def refresh_license_texts(
    session: httpx.Client, identifiers: set[str], folder: Path = LICENSES
) -> None:
    """Keep one SPDX text per license identifier in use, where SPDX has one."""
    folder.mkdir(parents=True, exist_ok=True)
    for path in folder.glob("*.txt"):
        if path.stem not in identifiers:
            path.unlink()
    for identifier in sorted(identifiers):
        path = folder / f"{identifier}.txt"
        if path.exists() or not re.fullmatch(r"[A-Za-z0-9.+-]+", identifier):
            continue
        response = _send(session, "GET", f"{SPDX}/{identifier}.txt")
        if response.status_code == 200:
            path.write_bytes(response.content)
        elif response.status_code != 404:
            raise SourceError(f"SPDX returned HTTP {response.status_code}.")


def refresh(session: httpx.Client) -> dict:
    """Describe the bundled image and VM runtime, and pin their copyleft sources."""
    from scripts.fetch_sandbox_image import ensure

    config, files = image_files(
        ensure(), lambda name: name in ("etc/alpine-release", "lib/apk/db/installed")
    )
    environment = dict(
        item.split("=", 1) for item in config["config"].get("Env", []) if "=" in item
    )
    image_alpine = files["etc/alpine-release"].decode().strip()
    image_packages = apk_packages(files["lib/apk/db/installed"].decode())
    smol = smol_folder()
    guest_alpine, guest_packages = guest_filesystem(smol)
    sources = {
        "format": FORMAT,
        "inputs": inputs(smol),
        "image": {
            "reference": f"docker.io/{IMAGE_REPOSITORY}:{IMAGE_TAG}",
            "platform": "linux/arm64",
            "python": environment.get("PYTHON_VERSION", ""),
            "alpine": image_alpine,
            "packages": image_packages,
        },
        "guest": {"alpine": guest_alpine, "packages": guest_packages},
        "components": [
            *runtime_components(session, smol),
            *alpine_components(session, guest_packages, guest_alpine, "vm-guest"),
            *alpine_components(session, image_packages, image_alpine, "python-image"),
        ],
    }
    validate_sources(sources)
    refresh_license_texts(session, referenced_licenses(sources))
    SOURCES.write_text(json.dumps(sources, indent=2) + "\n", encoding="utf-8")
    return sources


def validate_sources(sources: dict) -> None:
    """Every source file has a name, an HTTPS location, a size and a digest."""
    try:
        if sources["format"] != FORMAT or not sources["components"]:
            raise ValueError()
        for component in sources["components"]:
            if not re.fullmatch(r"[a-z-]+/[A-Za-z0-9._+-]+", component["folder"]):
                raise ValueError()
            for item in component["files"]:
                digest = item.get("sha512") or item.get("sha256")
                length = 128 if "sha512" in item else 64
                if (
                    not re.fullmatch(r"[A-Za-z0-9._+-]+", item["name"])
                    or not item["url"].startswith("https://")
                    or type(item["size"]) is not int
                    or item["size"] <= 0
                    or not re.fullmatch(rf"[0-9a-f]{{{length}}}", digest or "")
                ):
                    raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SourceError("The third-party source list is malformed.") from None


def python_notices(image: Path) -> tuple[dict[str, bytes], list[dict[str, str]]]:
    """CPython's license, and each Python distribution in the image with its own."""
    _, files = image_files(image, lambda name: bool(PYTHON_FILES.fullmatch(name)))
    copies, distributions = {}, []
    for name, data in sorted(files.items()):
        relative = PYTHON_FILES.fullmatch(name).group(1).removeprefix("site-packages/")
        if relative.endswith("/METADATA"):
            metadata = email.parser.BytesHeaderParser().parsebytes(data)
            license = metadata.get("License-Expression") or metadata.get("License", "")
            distributions.append(
                {
                    "name": metadata.get("Name", ""),
                    "version": metadata.get("Version", ""),
                    "license": license if len(license) <= 60 else "",
                    "folder": relative.removesuffix("/METADATA"),
                }
            )
        else:
            copies[relative] = data
    return copies, distributions


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _short(revision: str) -> str:
    return f"`{revision[:12]}`" if revision else ""


def _components_table(sources: dict) -> list[str]:
    lines = [
        "| Folder | Component | Version | Revision | Licenses | For |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for component in sources["components"]:
        lines.append(
            f"| `{component['folder']}` | {component['name']} | "
            f"{component['version']} | {_short(component.get('revision', ''))} | "
            f"{_cell(', '.join(component['licenses']))} | {_cell(component['for'])} |"
        )
    return lines


def _packages_table(packages: list[dict]) -> list[str]:
    lines = [
        "| Package | Version | License | Origin | aports commit |",
        "| --- | --- | --- | --- | --- |",
    ]
    for package in packages:
        lines.append(
            f"| {package['name']} | {package['version']} | "
            f"{_cell(package['license'])} | {package['origin']} | "
            f"{_short(package['commit'])} |"
        )
    return lines


def render_notices(
    sources: dict,
    version: str,
    distributions: list[dict[str, str]],
    python_files: set[str],
    texts: list[str],
) -> str:
    image, guest = sources["image"], sources["guest"]
    smol = sources["inputs"]["smolmachines"]
    missing = sorted(referenced_licenses(sources) - set(texts))
    lines = [
        "# Third-party notices",
        "",
        f"Vikram {version} includes the third-party software below in its Python "
        "sandbox: the virtual machine runtime that isolates code, and the Linux "
        "image the virtual machine boots. Each component keeps its own license; "
        "the license texts are in `licenses/`. Vikram itself is released under "
        "the MIT License.",
        "",
        "## Source code",
        "",
        "The complete corresponding source code of every GPL-, LGPL- and "
        "MPL-licensed component below is published beside this release as "
        f"`{sources_asset(version)}`:",
        "",
        f"<{sources_url(version)}>",
        "",
        "Each folder in it holds one component: its upstream source, and the "
        "build files and patches its distributor applied. "
        f"[`{SOURCES_FILE}`]({SOURCES_FILE}) lists every file, where it came "
        "from, and its digest.",
        "",
        *_components_table(sources),
        "",
        "## Virtual machine runtime",
        "",
        f"smolmachines {smol} (Apache-2.0) runs the sandbox's virtual machines. "
        "Its own notices list the components it bundles, each with its license "
        "and source: "
        "[licenses/smolmachines/THIRD_PARTY_NOTICES.md]"
        "(licenses/smolmachines/THIRD_PARTY_NOTICES.md). Their license texts "
        "are in `licenses/smolmachines/licenses/`.",
        "",
        f"Its guest filesystem holds Alpine Linux {guest['alpine']} and the "
        "packages below. Each package's build recipe is in [Alpine's aports]"
        f"({ALPINE}), under its origin, at the commit shown.",
        "",
        *_packages_table(guest["packages"]),
        "",
        "## Python image",
        "",
        f"The sandbox boots `{image['reference']}` for {image['platform']}, "
        f"pinned to `{sources['inputs']['image']}`. It holds:",
        "",
    ]
    if "LICENSE.txt" in python_files:
        lines.append(
            f"- CPython {image['python']}: "
            "[licenses/python-image/LICENSE.txt](licenses/python-image/LICENSE.txt)"
        )
    for distribution in distributions:
        names = sorted(
            name
            for name in python_files
            if name.startswith(f"{distribution['folder']}/licenses/")
        )
        if not names:
            continue
        # The distribution's own license sits directly in its licenses folder.
        own = [name for name in names if name.count("/") == 2] or names
        main = next(
            (
                name
                for name in own
                if name.rsplit("/", 1)[1].upper().startswith(("LICENSE", "COPYING"))
            ),
            own[0],
        )
        license = f" ({distribution['license']})" if distribution["license"] else ""
        line = (
            f"- {distribution['name']} {distribution['version']}{license}: "
            f"[licenses/python-image/{main}](licenses/python-image/{main})"
        )
        if len(names) > 1:
            line += ", with the licenses of the packages it vendors beside it"
        lines.append(line)
    lines += [
        f"- Alpine Linux {image['alpine']} and these packages, whose build "
        "recipes are in Alpine's aports as above:",
        "",
        *_packages_table(image["packages"]),
        "",
        "## License texts",
        "",
        "| License | Text |",
        "| --- | --- |",
        *(f"| {text} | [licenses/{text}.txt](licenses/{text}.txt) |" for text in texts),
    ]
    if missing:
        names = ", ".join(f"`{name}`" for name in missing)
        lines += [
            "",
            f"These identifiers, as the packages record them, have no standard "
            f"license text: {names}. Each package's own terms are in its source.",
        ]
    return "\n".join(lines) + "\n"


def write_notices(
    destination: Path,
    version: str,
    sources: dict,
    image: Path,
    smol: Path,
    licenses: Path = LICENSES,
) -> None:
    """Write the notices folder: the document, the source list and license texts."""
    validate_sources(sources)
    python_files, distributions = python_notices(image)
    texts = sorted(
        identifier
        for identifier in referenced_licenses(sources)
        if (licenses / f"{identifier}.txt").is_file()
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".notices-", dir=destination.parent))
    try:
        shutil.copytree(smol / "licenses", staging / "licenses/smolmachines/licenses")
        shutil.copyfile(
            smol / "THIRD_PARTY_NOTICES.md",
            staging / "licenses/smolmachines/THIRD_PARTY_NOTICES.md",
        )
        for relative, data in python_files.items():
            target = staging / "licenses/python-image" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        for identifier in texts:
            shutil.copyfile(
                licenses / f"{identifier}.txt",
                staging / f"licenses/{identifier}.txt",
            )
        (staging / SOURCES_FILE).write_text(
            json.dumps(sources, indent=2) + "\n", encoding="utf-8"
        )
        (staging / NOTICES_FILE).write_text(
            render_notices(sources, version, distributions, set(python_files), texts),
            encoding="utf-8",
        )
        if destination.exists():
            shutil.rmtree(destination)
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def project_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def build_notices(version: str, destination: Path = NOTICES) -> Path:
    """Write the notices the desktop build bundles, after checking the pins."""
    from scripts.fetch_sandbox_image import ensure

    sources = json.loads(SOURCES.read_text(encoding="utf-8"))
    smol = smol_folder()
    check_inputs(sources, smol)
    write_notices(destination, version, sources, ensure(), smol)
    check_folder(destination, version)
    return destination


def _check(read: Callable[[str], bytes | None], version: str) -> tuple[str, dict]:
    document, listing = read(NOTICES_FILE), read(SOURCES_FILE)
    if document is None or listing is None:
        raise SourceError("The app has no third-party notices. Rebuild it.")
    try:
        text, sources = document.decode(), json.loads(listing)
    except ValueError:
        raise SourceError("The app's third-party notices are malformed.") from None
    if f"Vikram {version} " not in text or sources_url(version) not in text:
        raise SourceError(
            f"The app's third-party notices are not for Vikram {version}. Rebuild it."
        )
    validate_sources(sources)
    for target in re.findall(r"\]\((licenses/[^)]+)\)", text):
        if read(target) is None:
            raise SourceError(f"The third-party notices name a missing {target}.")
    return text, sources


def check_folder(folder: Path, version: str) -> dict:
    """Check a notices folder and return its source list."""

    def read(name: str) -> bytes | None:
        path = folder / name
        return path.read_bytes() if path.is_file() and not path.is_symlink() else None

    return _check(read, version)[1]


def check_notices(app: Path, version: str) -> dict:
    """Check the notices inside a built Vikram.app and return its source list."""
    return check_folder(app / APP_NOTICES, version)


def archive_notices(archive: Path, version: str) -> tuple[str, dict]:
    """The notices document and source list inside an installer archive."""
    prefix = f"{ARCHIVE_ROOT}/Contents/Resources/Vikram.app/{APP_NOTICES}/"
    try:
        with zipfile.ZipFile(archive) as source:
            names = set(source.namelist())

            def read(name: str) -> bytes | None:
                member = prefix + name
                return source.read(member) if member in names else None

            return _check(read, version)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError):
        raise SourceError("The installer archive could not be read.") from None


class _Transient(Exception):
    """A download the host interrupted or refused for now."""


def _download_once(session: httpx.Client, item: dict, path: Path) -> None:
    algorithm = "sha512" if "sha512" in item else "sha256"
    digest, size = hashlib.new(algorithm), 0
    try:
        with session.stream("GET", item["url"]) as response:
            status = response.status_code
            if status != 200:
                failure = _Transient if status in TRANSIENT else SourceError
                raise failure(f"{item['url']} returned HTTP {status}.")
            with path.open("wb") as stream:
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > item["size"]:
                        raise SourceError(f"{item['url']} is larger than pinned.")
                    digest.update(chunk)
                    stream.write(chunk)
    except httpx.TransportError:
        raise _Transient(f"{item['url']} could not be downloaded.") from None
    if size != item["size"] or digest.hexdigest() != item[algorithm]:
        raise SourceError(f"{item['url']} does not match its pinned digest.")


def _download(session: httpx.Client, item: dict, path: Path) -> None:
    """Download one pinned file, trying again while the host fails temporarily."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return _download_once(session, item, path)
        except _Transient as error:
            path.unlink(missing_ok=True)
            if attempt == ATTEMPTS:
                raise SourceError(str(error)) from None
        time.sleep(DELAY * attempt)


def render_readme(sources: dict, version: str) -> str:
    lines = [
        f"# Third-party sources for Vikram {version}",
        "",
        "This archive holds the complete corresponding source code of the GPL-, "
        "LGPL- and MPL-licensed components in Vikram's Python sandbox. "
        f"`{NOTICES_FILE}`, inside Vikram.app (`{APP_NOTICES}`) and beside this "
        "archive on the release page, lists every component and its license.",
        "",
        "For an Alpine Linux package, a folder holds the package's build recipe "
        "(`APKBUILD`) and patches from Alpine's aports repository at the commit "
        "shown, and the upstream files the recipe names, from Alpine's source "
        "mirror; Alpine's `abuild` builds it. For libkrunfw, it holds that "
        "repository at the commit shown: its Makefile names the Linux release it "
        "builds and the configuration and patches it applies. "
        f"`{SOURCES_FILE}` lists where each file came from and its digest.",
        "",
        *_components_table(sources),
    ]
    return "\n".join(lines) + "\n"


def _add(archive: tarfile.TarFile, name: str, data: bytes | Path) -> None:
    # Fixed owner, mode and time: the same sources always make the same archive.
    info = tarfile.TarInfo(name)
    info.mode = 0o644
    if isinstance(data, Path):
        info.size = data.stat().st_size
        with data.open("rb") as stream:
            archive.addfile(info, stream)
    else:
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))


def fetch_sources(
    session: httpx.Client, sources: dict, version: str, destination: Path
) -> Path:
    """Download every pinned source file, check it, and pack the release's archive."""
    validate_sources(sources)
    name = sources_asset(version)
    prefix = name.removesuffix(".tar")
    with tempfile.TemporaryDirectory(prefix=".sources-", dir=destination) as work:
        members = []
        for component in sources["components"]:
            for item in component["files"]:
                path = Path(work) / str(len(members))
                _download(session, item, path)
                members.append((f"{prefix}/{component['folder']}/{item['name']}", path))
        staged = Path(work) / name
        with tarfile.open(staged, "w", format=tarfile.PAX_FORMAT) as archive:
            _add(
                archive, f"{prefix}/README.md", render_readme(sources, version).encode()
            )
            listing = json.dumps(sources, indent=2) + "\n"
            _add(archive, f"{prefix}/{SOURCES_FILE}", listing.encode())
            for member, path in sorted(members):
                _add(archive, member, path)
        staged.replace(destination / name)
    return destination / name


def check_published(session: httpx.Client, sources: dict) -> int:
    """Check that every pinned source file is still published; return the total size."""
    validate_sources(sources)
    total = 0
    for component in sources["components"]:
        for item in component["files"]:
            size = _published_size(session, item["url"])
            if size is not None and size != item["size"]:
                raise SourceError(f"{item['url']} changed size since it was pinned.")
            total += item["size"]
    return total


def check_sources_archive(tarball: Path, sources: dict, version: str) -> None:
    """Check a packed sources archive against the pinned list, without a network.

    The release Mac downloads the sources (some hosts refuse data-center
    addresses) and the publish job checks its copy here: exactly the expected
    regular files, each with its pinned size and digest, plus the README and
    listing this version renders.
    """
    validate_sources(sources)
    prefix = sources_asset(version).removesuffix(".tar")
    expected = {
        f"{prefix}/README.md": render_readme(sources, version).encode(),
        f"{prefix}/{SOURCES_FILE}": (json.dumps(sources, indent=2) + "\n").encode(),
    }
    for component in sources["components"]:
        for item in component["files"]:
            expected[f"{prefix}/{component['folder']}/{item['name']}"] = item
    seen = set()
    try:
        with tarfile.open(tarball, "r:") as archive:
            for member in archive:
                if member.name in seen or member.name not in expected:
                    raise SourceError(
                        f"The sources archive has an unexpected {member.name}."
                    )
                if not member.isreg():
                    raise SourceError(
                        f"{member.name} in the sources archive is not a file."
                    )
                seen.add(member.name)
                want = expected[member.name]
                stream = archive.extractfile(member)
                if isinstance(want, bytes):
                    if stream.read(len(want) + 1) != want:
                        raise SourceError(f"{member.name} does not match this release.")
                    continue
                algorithm = "sha512" if "sha512" in want else "sha256"
                digest = hashlib.new(algorithm)
                if member.size != want["size"]:
                    raise SourceError(
                        f"{member.name} does not match its pinned digest."
                    )
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                if digest.hexdigest() != want[algorithm]:
                    raise SourceError(
                        f"{member.name} does not match its pinned digest."
                    )
    except (tarfile.TarError, OSError):
        raise SourceError("The sources archive could not be read.") from None
    if missing := sorted(set(expected) - seen):
        raise SourceError(f"The sources archive is missing {missing[0]}.")


def release_assets(
    archive: Path,
    version: str,
    destination: Path,
    session: httpx.Client | None = None,
    sources_archive: Path | None = None,
) -> list[tuple[Path, str]]:
    """The notices and the verified sources a release publishes beside the archive.

    With `sources_archive` (packed by the release Mac), that copy is checked
    offline instead of downloading the sources again.
    """
    document, sources = archive_notices(archive, version)
    notices = destination / notices_asset(version)
    notices.write_text(document, encoding="utf-8")
    if sources_archive is not None:
        check_sources_archive(sources_archive, sources, version)
        tarball = destination / sources_asset(version)
        shutil.copyfile(sources_archive, tarball)
    else:
        with contextlib.nullcontext(session) if session else client() as downloads:
            tarball = fetch_sources(downloads, sources, version, destination)
    return [(notices, "text/markdown; charset=utf-8"), (tarball, "application/x-tar")]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "refresh",
        help="pin the copyleft sources of the bundled image and VM runtime again",
    )
    commands.add_parser("notices", help="write the notices the desktop build bundles")
    download = commands.add_parser(
        "sources", help="download and check the third-party sources for an archive"
    )
    download.add_argument("archive", type=Path, help="Vikram-VERSION-macos-arm64.zip")
    download.add_argument(
        "--check",
        action="store_true",
        help="only check that every source is still published",
    )
    args = parser.parse_args()
    try:
        if args.command == "refresh":
            with client() as session:
                sources = refresh(session)
            files = sum(len(c["files"]) for c in sources["components"])
            print(
                f"Pinned {files} files for {len(sources['components'])} components "
                f"in {SOURCES.relative_to(ROOT)}."
            )
        elif args.command == "notices":
            print(build_notices(project_version()))
        else:
            match = re.fullmatch(r"Vikram-(.+)-macos-arm64\.zip", args.archive.name)
            if match is None:
                raise SourceError("Expected Vikram-VERSION-macos-arm64.zip.")
            version = match.group(1)
            _, sources = archive_notices(args.archive, version)
            with client() as session:
                if args.check:
                    total = check_published(session, sources)
                    print(
                        f"All {sum(len(c['files']) for c in sources['components'])} "
                        f"third-party source files ({total} bytes) are published."
                    )
                    return
                total = sum(
                    item["size"]
                    for component in sources["components"]
                    for item in component["files"]
                )
                ensure_space(args.archive.parent, 2 * total)
                print(fetch_sources(session, sources, version, args.archive.parent))
    except (SourceError, InstallError, OSError, httpx.HTTPError) as error:
        raise SystemExit(f"Third-party notices: {error}") from None


if __name__ == "__main__":
    main()
