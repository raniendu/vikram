"""Fetch and verify the pinned standalone interpreter used by extension workers.

The full upstream archive supplies machine-readable component licenses that are
absent from install-only archives. The build retains these with the runtime.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "scripts/third-party/extension-python.json"
DESTINATION = ROOT / "build/extension-python"
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_EXPANDED = 2 * 1024**3
MACH_O = {
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
}


def manifest() -> dict:
    value = json.loads(MANIFEST.read_text())
    if (
        value.get("format") != 1
        or not re.fullmatch(r"[0-9a-f]{64}", value.get("sha256", ""))
        or value.get("version") != ".".join(map(str, sys.version_info[:3]))
    ):
        raise ValueError(
            "The extension Python pin must match the core interpreter version exactly."
        )
    url = urlsplit(value["url"])
    if (
        url.scheme != "https"
        or url.hostname != "github.com"
        or not url.path.startswith(
            "/astral-sh/python-build-standalone/releases/download/"
        )
    ):
        raise ValueError(
            "The extension interpreter source is not the pinned upstream release."
        )
    return value


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _reserve(path: Path) -> None:
    if shutil.disk_usage(path).free < 24 * 1024**3:
        raise ValueError(
            "Extension packaging needs 24 GiB free to protect the 20 GiB reserve."
        )


def fetch_archive(pin: dict, path: Path) -> Path:
    if path.is_file() and digest(path) == pin["sha256"]:
        return path
    temporary = path.with_suffix(".download")
    try:
        total = 0
        with (
            urlopen(pin["url"], timeout=60) as response,
            temporary.open("wb") as output,
        ):
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_ARCHIVE:
                    raise ValueError(
                        "The extension interpreter archive exceeds its limit."
                    )
                _reserve(path.parent)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest(temporary) != pin["sha256"]:
            raise ValueError(
                "The extension interpreter checksum does not match its pin."
            )
        temporary.replace(path)
        return path
    finally:
        temporary.unlink(missing_ok=True)


def _extract_members(archive, destination: Path) -> None:
    total, count = 0, 0
    for member in archive:
        count += 1
        total += member.size
        if total > MAX_EXPANDED or count > 50_000:
            raise ValueError(
                "The extension interpreter archive expands beyond its limit."
            )
        # Python's data filter also rejects escaping links and special files.
        archive.extract(member, destination, filter="data")


def extract_archive(path: Path, destination: Path) -> None:
    if path.name.endswith(".tar.zst"):
        # macOS bsdtar decodes zstd; reserialize as an uncompressed tar stream
        # so Python's extraction filter is still the path/link security boundary.
        process = subprocess.Popen(
            ["/usr/bin/tar", "-cf", "-", "@" + str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                _extract_members(archive, destination)
            if process.wait(timeout=30):
                raise ValueError(
                    "The extension interpreter archive could not be decoded."
                )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
    else:
        with tarfile.open(path, "r:*") as archive:
            _extract_members(archive, destination)


def collect_licenses(extracted: Path, runtime: Path) -> None:
    metadata = json.loads((extracted / "python/PYTHON.json").read_text())
    if (
        metadata.get("python_version") != manifest()["version"]
        or metadata.get("target_triple") != manifest()["target"]
    ):
        raise ValueError("Upstream interpreter metadata disagrees with the pin.")
    names, identifiers = set(), set(metadata.get("licenses", []))
    system_zlib = []
    if metadata.get("license_path"):
        names.add(metadata["license_path"])
    for candidates in metadata.get("build_info", {}).get("extensions", {}).values():
        for candidate in candidates:
            identifiers.update(candidate.get("licenses", []))
            paths = candidate.get("license_paths", candidate.get("license_path", []))
            names.update([paths] if isinstance(paths, str) else paths)
            if "licenses/LICENSE.zlib-ng.txt" in paths:
                links = [
                    link
                    for link in candidate.get("links", [])
                    if link.get("name") == "z"
                ]
                system_zlib.append(
                    bool(links)
                    and all(
                        link.get("system") is True
                        and not link.get("path_static")
                        and not link.get("path_dynamic")
                        for link in links
                    )
                )
    # These require a corresponding source bundle in the release manifest. The
    # selected macOS build is expected not to include these compiled libraries.
    if any("GPL" in identifier for identifier in identifiers):
        raise ValueError(
            "This interpreter contains GPL/LGPL components. Add their exact source artifacts to the release source manifest before packaging."
        )
    notices = runtime / "licenses"
    notices.mkdir()
    omitted_system = []
    for name in sorted(names):
        candidate = (extracted / "python" / name).resolve()
        # Upstream metadata lists zlib-ng as an alternative provider for `z`.
        # This macOS artifact links system libz and contains no zlib-ng binary.
        if (
            name == "licenses/LICENSE.zlib-ng.txt"
            and not candidate.exists()
            and system_zlib
            and all(system_zlib)
        ):
            omitted_system.append(name)
            continue
        if (
            not candidate.is_relative_to((extracted / "python").resolve())
            or not candidate.is_file()
        ):
            raise ValueError("An upstream interpreter component license is missing.")
        target = notices / (
            hashlib.sha256(name.encode()).hexdigest()[:12] + "-" + candidate.name
        )
        shutil.copy2(candidate, target)
    # pip's vendored Python dependencies have their own notices and include MPL
    # source. Retain exact source files for the release source-archive builder.
    packages = (
        runtime
        / "lib"
        / ("python" + ".".join(manifest()["version"].split(".")[:2]))
        / "site-packages"
    )
    sources = []
    if packages.is_dir():
        for source in sorted(packages.rglob("*")):
            if not source.is_file() or source.is_symlink():
                continue
            relative = source.relative_to(packages)
            license_file = source.name.lower().startswith(
                ("license", "copying", "notice")
            )
            if license_file:
                target = notices / "python-packages" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            if source.suffix in {".py", ".pem", ".typed"} or license_file:
                target = notices / "sources/python-packages" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                sources.append({"path": str(relative), "sha256": digest(source)})
    (notices / "sources.json").write_text(
        json.dumps(
            {"format": 1, "runtime_sha256": manifest()["sha256"], "files": sources},
            indent=2,
        )
        + "\n"
    )
    if not names:
        raise ValueError("The interpreter archive has no component license metadata.")
    shutil.copy2(extracted / "python/PYTHON.json", runtime / "PYTHON.json")
    (notices / "components.json").write_text(
        json.dumps(
            {
                "licenses": sorted(identifiers),
                "files": sorted(names - set(omitted_system)),
                "unbundled_system_alternatives": omitted_system,
            },
            indent=2,
        )
        + "\n"
    )


def check_runtime(runtime: Path, *, expected: dict | None = None) -> None:
    pin = expected or manifest()
    try:
        recorded = json.loads((runtime / "manifest.json").read_text())
    except (OSError, ValueError):
        raise ValueError(
            "The bundled extension interpreter manifest is missing."
        ) from None
    if recorded != pin or not (runtime / "licenses/components.json").is_file():
        raise ValueError(
            "The bundled extension interpreter or notices do not match the pin."
        )
    executable = runtime / pin["executable"]
    if (
        not executable.is_file()
        or not os.access(executable, os.X_OK)
        or not executable.resolve().is_relative_to(runtime.resolve())
    ):
        raise ValueError(
            "The extension Python executable is missing or not executable."
        )
    result = subprocess.run(
        [
            str(executable),
            "-I",
            "-c",
            "import json,sys,ssl,sqlite3,venv,ensurepip; print(json.dumps(list(sys.version_info[:3])))",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
        env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"},
    )
    if result.returncode or result.stdout.strip() != json.dumps(
        list(map(int, pin["version"].split(".")))
    ):
        raise ValueError(
            "The bundled extension interpreter failed its isolated startup check."
        )


def ensure(destination: Path = DESTINATION) -> Path:
    pin = manifest()
    destination.parent.mkdir(parents=True, exist_ok=True)
    _reserve(destination.parent)
    # Rebuild from the digest-checked archive instead of trusting a mutable cache.
    archive = fetch_archive(pin, destination.parent / "extension-python.tar.zst")
    with tempfile.TemporaryDirectory(
        prefix="extension-python-", dir=destination.parent
    ) as temporary:
        extracted = Path(temporary) / "extracted"
        extracted.mkdir()
        extract_archive(archive, extracted)
        runtime = extracted / pin["installation"]
        collect_licenses(extracted, runtime)
        (runtime / "manifest.json").write_text(json.dumps(pin, indent=2) + "\n")
        check_runtime(runtime, expected=pin)
        previous = destination.with_name(destination.name + ".previous")
        if previous.exists():
            shutil.rmtree(previous)
        if destination.exists():
            destination.rename(previous)
        try:
            runtime.rename(destination)
        except BaseException:
            if previous.exists():
                previous.rename(destination)
            raise
    return destination


def check_bundle(app: Path) -> None:
    runtime = app / "Contents/Resources/extension-python"
    check_runtime(runtime)
    workers = app / "Contents/Resources/extension-worker"
    for name in ("extension_sdk.py", "extension_worker.py"):
        if not (workers / "vikram" / name).is_file():
            raise ValueError("The bundled extension worker is missing.")
    script = "import sys; sys.path.insert(0, sys.argv[1]); from vikram.extension_sdk import API_VERSION, Extension; import vikram.extension_worker; assert API_VERSION == 1; assert Extension().describe()['api_version'] == 1"
    checked = subprocess.run(
        [str(runtime / manifest()["executable"]), "-I", "-c", script, str(workers)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=20,
        env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"},
    )
    if checked.returncode:
        raise ValueError(
            "The bundled extension worker failed its isolated startup check."
        )


def sign_runtime(runtime: Path) -> None:
    """Sign every nested Mach-O before signing the containing app bundle."""
    for path in sorted(runtime.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as stream:
            magic = stream.read(4)
        if magic in MACH_O:
            subprocess.run(
                ["/usr/bin/codesign", "--force", "--sign", "-", str(path)], check=True
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check-manifest", action="store_true")
    parser.add_argument("--check-runtime", type=Path)
    args = parser.parse_args()
    if args.check_manifest:
        print(json.dumps(manifest(), indent=2))
    elif args.check_runtime:
        check_runtime(args.check_runtime)
        print("Extension interpreter startup and notices verified.")
    else:
        print(ensure())


if __name__ == "__main__":
    main()
