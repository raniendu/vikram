"""Self-update for the vikram CLI.

Mirrors what ``install.sh`` does at install time: locates the
on-disk source checkout, fast-forwards it, and reinstalls the ``vikram`` uv
tool from that checkout.

This module is import-light on purpose — no agent, pydantic-ai, or settings
imports — so ``vikram update`` runs even on a machine whose model provider
config is broken.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_INSTALL_DIR = Path.home() / ".local" / "share" / "vikram"
DEFAULT_PYTHON_VERSION = "3.13"


def _config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "vikram"


def metadata_path() -> Path:
    return _config_dir() / "install.toml"


def _parse_toml(text: str) -> dict[str, Any]:
    import tomllib

    return tomllib.loads(text)


def load_metadata(path: Path | None = None) -> dict[str, Any]:
    path = path or metadata_path()
    if not path.is_file():
        return {}
    try:
        return _parse_toml(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _toml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_metadata(data: dict[str, Any], path: Path | None = None) -> Path:
    path = path or metadata_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Written by vikram install/update — do not edit by hand.\n"]
    for key in ("source_dir", "installed_at", "python_version", "git_sha"):
        value = data.get(key)
        if value is None:
            continue
        lines.append(f"{key} = {_toml_quote(str(value))}\n")
    path.write_text("".join(lines), encoding="utf-8")
    return path


def resolve_source(explicit: str | None = None) -> Path:
    """Pick the source checkout.

    Priority: --source flag > metadata file > $VIKRAM_INSTALL_DIR > default.
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    meta = load_metadata()
    if meta.get("source_dir"):
        candidates.append(Path(str(meta["source_dir"])).expanduser())
    env_dir = os.environ.get("VIKRAM_INSTALL_DIR")
    if env_dir:
        candidates.append(Path(env_dir).expanduser())
    candidates.append(DEFAULT_INSTALL_DIR)

    for candidate in candidates:
        if (candidate / ".git").exists():
            return candidate

    raise UpdateError(
        "No vikram checkout found. Looked in:\n"
        + "\n".join(f"  - {c}" for c in candidates)
        + "\nRe-run install.sh, or pass --source PATH."
    )


class UpdateError(RuntimeError):
    """Raised when the update cannot proceed; surfaced to the user."""


def _run(
    cmd: Sequence[str],
    *,
    cwd: Path | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def _git(args: Sequence[str], *, cwd: Path, capture: bool = True) -> str:
    result = _run(["git", *args], cwd=cwd, capture=capture)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise UpdateError(f"git {' '.join(args)} failed: {stderr}")
    return (result.stdout or "").strip()


def _has_uncommitted_changes(source: Path) -> bool:
    return bool(_git(["status", "--porcelain"], cwd=source))


def _current_branch(source: Path) -> str | None:
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=source)
    return None if branch == "HEAD" else branch


def _upstream_sha(source: Path, branch: str) -> str:
    return _git(["rev-parse", f"origin/{branch}"], cwd=source)


def _commit_count(source: Path, old: str, new: str) -> int:
    raw = _git(["rev-list", "--count", f"{old}..{new}"], cwd=source)
    try:
        return int(raw)
    except ValueError:
        return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vikram update",
        description="Pull the latest vikram source and reinstall the CLI.",
        epilog=("Rollback: git -C <source> reset --hard <good-sha> && vikram update"),
    )
    parser.add_argument(
        "--source",
        default=None,
        help=(
            "Path to the vikram repo checkout to update. Overrides the install "
            "metadata and $VIKRAM_INSTALL_DIR."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Show what would change without pulling or reinstalling.",
    )
    parser.add_argument(
        "--ref",
        default=None,
        help=(
            "Optional branch/tag/SHA to check out before reinstalling. "
            "Defaults to fast-forwarding the current branch."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON status object instead of human-readable text.",
    )
    return parser


def _emit(text: str, *, json_mode: bool, payload: dict[str, Any]) -> None:
    if json_mode:
        print(json.dumps(payload))
    else:
        print(text)


def run(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        source = resolve_source(args.source)
    except UpdateError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if shutil.which("git") is None:
        print("git is not on PATH; cannot update.", file=sys.stderr)
        return 1
    if shutil.which("uv") is None:
        print("uv is not on PATH; cannot reinstall the tool.", file=sys.stderr)
        return 1

    try:
        if _has_uncommitted_changes(source):
            print(
                f"{source} has uncommitted changes. Commit, stash, or discard "
                "them and rerun.",
                file=sys.stderr,
            )
            return 1

        old_sha = _git(["rev-parse", "HEAD"], cwd=source)
        branch = _current_branch(source)

        if args.ref:
            _git(["fetch", "--quiet", "origin", args.ref], cwd=source)
            _git(["checkout", "--quiet", args.ref], cwd=source)
            new_branch = _current_branch(source)
            if new_branch:
                _git(["pull", "--quiet", "--ff-only"], cwd=source)
            target_sha = _git(["rev-parse", "HEAD"], cwd=source)
        else:
            _git(["fetch", "--quiet", "origin"], cwd=source)
            if branch is None:
                print(
                    f"{source} is in a detached HEAD state; pass --ref to "
                    "specify what to update to.",
                    file=sys.stderr,
                )
                return 1
            target_sha = _upstream_sha(source, branch)

        meta = load_metadata()
        installed_sha = str(meta.get("git_sha") or "")
        installed_is_stale = bool(installed_sha) and installed_sha != old_sha
        source_is_current = target_sha == old_sha and not args.ref

        if source_is_current and not installed_is_stale:
            _emit(
                f"Already up to date at {old_sha[:12]}.",
                json_mode=args.json,
                payload={
                    "status": "up_to_date",
                    "sha": old_sha,
                    "source": str(source),
                },
            )
            return 0

        if args.check:
            if source_is_current and installed_is_stale:
                _emit(
                    (
                        "Installed tool is stale: "
                        f"{installed_sha[:12]} → {old_sha[:12]}"
                    ),
                    json_mode=args.json,
                    payload={
                        "status": "pending_reinstall",
                        "old_sha": installed_sha,
                        "new_sha": old_sha,
                        "source": str(source),
                    },
                )
            else:
                ahead = _commit_count(source, old_sha, target_sha)
                _emit(
                    (
                        f"{ahead} commit(s) pending: "
                        f"{old_sha[:12]} → {target_sha[:12]}"
                    ),
                    json_mode=args.json,
                    payload={
                        "status": "pending",
                        "old_sha": old_sha,
                        "new_sha": target_sha,
                        "commits": ahead,
                        "source": str(source),
                    },
                )
            return 0

        if args.ref:
            new_sha = target_sha
        elif not source_is_current:
            _git(["pull", "--quiet", "--ff-only"], cwd=source)
            new_sha = _git(["rev-parse", "HEAD"], cwd=source)
        else:
            new_sha = old_sha

        python_version = str(meta.get("python_version") or DEFAULT_PYTHON_VERSION)
        vikram_dir = source
        if not (vikram_dir / "pyproject.toml").is_file():
            print(
                f"Expected {vikram_dir}/pyproject.toml; aborting reinstall.",
                file=sys.stderr,
            )
            return 1

        # --reinstall-package vikram invalidates uv's cached wheel for the
        # vikram package so the rebuild actually picks up new source. Without
        # it, uv reuses a cached wheel for vikram==0.1.0 even when the local
        # source has changed, and the update silently no-ops.
        install_cmd = [
            "uv",
            "tool",
            "install",
            "--force",
            "--reinstall-package",
            "vikram",
            "--python",
            python_version,
            "--from",
            str(vikram_dir),
            "vikram",
        ]
        result = _run(install_cmd)
        if result.returncode != 0:
            print("uv tool install failed.", file=sys.stderr)
            return result.returncode

        write_metadata(
            {
                "source_dir": str(source),
                "installed_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "python_version": python_version,
                "git_sha": new_sha,
            }
        )

        if source_is_current and installed_is_stale:
            _emit(
                f"vikram: {installed_sha[:12]} → {new_sha[:12]} (reinstalled)",
                json_mode=args.json,
                payload={
                    "status": "reinstalled",
                    "old_sha": installed_sha,
                    "new_sha": new_sha,
                    "source": str(source),
                },
            )
        else:
            ahead = _commit_count(source, old_sha, new_sha)
            _emit(
                f"vikram: {old_sha[:12]} → {new_sha[:12]} ({ahead} commit(s))",
                json_mode=args.json,
                payload={
                    "status": "updated",
                    "old_sha": old_sha,
                    "new_sha": new_sha,
                    "commits": ahead,
                    "source": str(source),
                },
            )
        return 0
    except UpdateError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run())
