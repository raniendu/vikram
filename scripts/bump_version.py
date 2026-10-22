"""Set the release version in every version file and lockfile, or check them.

    uv run python scripts/bump_version.py 0.2.0
    uv run python scripts/bump_version.py --check [0.2.0]

A bump edits only the version strings, so lockfiles keep every dependency pin.
"""

import argparse
import json
import os
import re
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vikram.installation import InstallError, version_tuple  # noqa: E402


def _locked(name: str, editable: bool) -> Callable[[str], str]:
    def read(text: str) -> str:
        (version,) = [
            package["version"]
            for package in tomllib.loads(text)["package"]
            if package["name"] == name
            and (package.get("source") == {"editable": "."}) == editable
        ]
        return version

    return read


@dataclass(frozen=True)
class Place:
    path: str
    # Matches exactly once; group 1 is the version.
    pattern: str
    # Parses the whole file and returns the version this place holds.
    read: Callable[[str], str]
    detail: str = ""

    @property
    def label(self) -> str:
        return f"{self.path} {self.detail}".strip()


PLACES = (
    Place(
        "pyproject.toml",
        r'^version = "([^"]*)"$',
        lambda text: tomllib.loads(text)["project"]["version"],
    ),
    Place(
        "uv.lock",
        r'^name = "vikram"\nversion = "([^"]*)"\nsource = \{ editable = "\." \}$',
        _locked("vikram", editable=True),
    ),
    Place(
        "apps/desktop/package.json",
        r'^  "version": "([^"]*)",$',
        lambda text: json.loads(text)["version"],
    ),
    Place(
        "apps/desktop/package-lock.json",
        r'^  "name": "vikram-desktop",\n  "version": "([^"]*)",$',
        lambda text: json.loads(text)["version"],
        "(top level)",
    ),
    Place(
        "apps/desktop/package-lock.json",
        r'^      "name": "vikram-desktop",\n      "version": "([^"]*)",$',
        lambda text: json.loads(text)["packages"][""]["version"],
        "(root package)",
    ),
    Place(
        "apps/desktop/src-tauri/Cargo.toml",
        r'^version = "([^"]*)"$',
        lambda text: tomllib.loads(text)["package"]["version"],
    ),
    Place(
        "apps/desktop/src-tauri/Cargo.lock",
        r'^name = "vikram-desktop"\nversion = "([^"]*)"$',
        _locked("vikram-desktop", editable=False),
    ),
    Place(
        "apps/desktop/src-tauri/tauri.conf.json",
        r'^  "version": "([^"]*)",$',
        lambda text: json.loads(text)["version"],
    ),
)


def _read(place: Place, text: str) -> str | None:
    try:
        return place.read(text)
    except (KeyError, TypeError, ValueError):
        return None


def current_versions(root: Path = ROOT) -> dict[str, str]:
    """Each place's version; the pattern and the file's parser must agree."""
    versions = {}
    for place in PLACES:
        text = (root / place.path).read_text()
        matches = re.findall(place.pattern, text, flags=re.MULTILINE)
        if len(matches) != 1 or matches[0] != _read(place, text):
            raise SystemExit(f"Cannot find the release version in {place.label}.")
        versions[place.label] = matches[0]
    return versions


def agreed_version(root: Path = ROOT) -> str:
    versions = current_versions(root)
    if len(set(versions.values())) != 1:
        details = "\n".join(f"  {label}: {value}" for label, value in versions.items())
        raise SystemExit(f"The versions disagree:\n{details}")
    return next(iter(versions.values()))


def _replacement(version: str) -> Callable[[re.Match], str]:
    def replace(match: re.Match) -> str:
        whole, offset = match.group(0), match.start(0)
        return (
            whole[: match.start(1) - offset] + version + whole[match.end(1) - offset :]
        )

    return replace


def bump(version: str, root: Path = ROOT) -> list[Path]:
    """Set version everywhere, changing no file unless every edit succeeds."""
    current = agreed_version(root)
    if version_tuple(version) <= version_tuple(current):
        raise SystemExit(
            f"{version} is not newer than {current}. Every release needs a new version."
        )
    texts: dict[Path, str] = {}
    for place in PLACES:
        path = root / place.path
        if path not in texts:
            texts[path] = path.read_text()
        texts[path], count = re.subn(
            place.pattern, _replacement(version), texts[path], flags=re.MULTILINE
        )
        if count != 1:
            raise SystemExit(f"Cannot find the release version in {place.label}.")
    for place in PLACES:
        if _read(place, texts[root / place.path]) != version:
            raise SystemExit(f"Editing {place.label} did not set {version}.")
    for path, text in texts.items():
        staged = path.with_name(f".{path.name}.bump")
        try:
            staged.write_text(text)
            os.chmod(staged, path.stat().st_mode)
            os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)
    return list(texts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("version", nargs="?", help="the MAJOR.MINOR.PATCH version")
    parser.add_argument(
        "--check",
        action="store_true",
        help="print the version if every file agrees (on VERSION, when given)",
    )
    args = parser.parse_args()
    if args.version is None and not args.check:
        parser.error("give the new version, for example 0.2.0")
    try:
        if args.version is not None:
            version_tuple(args.version)
    except InstallError as error:
        raise SystemExit(f"{args.version}: {error}") from None
    if args.check:
        version = agreed_version(ROOT)
        if args.version not in (None, version):
            raise SystemExit(
                f"The tag says {args.version}, but the files say {version}. Bump "
                "the version in a pull request, then tag its merge commit."
            )
        print(version)
        return
    for path in bump(args.version, ROOT):
        print(f"Set {args.version} in {path.relative_to(ROOT)}")
    print(
        f"Next: run `uv sync --locked`, merge the change through a pull request, "
        f"then push the tag v{args.version} on the merge commit to publish it."
    )


if __name__ == "__main__":
    main()
