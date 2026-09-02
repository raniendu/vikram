"""Launch the desktop app.

Launching from the terminal is not a convenience here, it is the fix for a real
problem: macOS apps started from Finder or Spotlight do not inherit the login
shell's PATH, so ``~/.local/bin/vikram-api`` -- where ``uv tool install`` puts
it -- is invisible to them. Starting from a shell means this process *can* see
it, so it resolves the absolute path and hands it to the app in
``VIKRAM_API_BIN``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

API_BIN_ENV = "VIKRAM_API_BIN"
APP_ENV = "VIKRAM_GUI_APP"

BUNDLE_NAME = "Vikram Studio"

# Installed locations, in preference order. The checkout's build output is
# checked after these; see find_bundle.
BUNDLE_CANDIDATES = (
    f"~/Applications/{BUNDLE_NAME}.app",
    f"/Applications/{BUNDLE_NAME}.app",
)


def find_api_binary() -> Path | None:
    """Absolute path to ``vikram-api``, or None."""
    explicit = os.environ.get(API_BIN_ENV)
    if explicit and Path(explicit).is_file():
        return Path(explicit)

    found = shutil.which("vikram-api")
    if found:
        return Path(found).resolve()

    for candidate in (
        Path.home() / ".local" / "bin" / "vikram-api",
        Path.home() / ".cargo" / "bin" / "vikram-api",
        Path("/opt/homebrew/bin/vikram-api"),
        Path("/usr/local/bin/vikram-api"),
    ):
        if candidate.is_file():
            return candidate
    return None


def studio_log_path() -> Path:
    """Where the detached app's output goes, so it stays readable."""
    state_dir = os.environ.get("VIKRAM_STATE_DIR")
    root = Path(state_dir) if state_dir else Path.home() / ".vikram"
    return root / "studio.log"


def bundle_build_output() -> Path:
    """Where `npm run tauri build` leaves the app inside a checkout."""
    return (
        repo_gui_dir()
        / "src-tauri"
        / "target"
        / "release"
        / "bundle"
        / "macos"
        / f"{BUNDLE_NAME}.app"
    )


def find_bundle() -> Path | None:
    """Locate the app, preferring an installed copy over a fresh build.

    The checkout's build output counts: having just built it there is the
    normal case when working on the app, and requiring a copy into
    ~/Applications first is friction with no purpose.
    """
    explicit = os.environ.get(APP_ENV)
    if explicit and Path(explicit).exists():
        return Path(explicit)
    for candidate in BUNDLE_CANDIDATES:
        path = Path(candidate).expanduser()
        if path.exists():
            return path
    built = bundle_build_output()
    return built if built.exists() else None


def repo_gui_dir() -> Path:
    """The ``gui/`` sources, from a checkout or from the recorded install.

    Mirrors how ``settings._resolve_spec_root`` finds ``spec/``: a checkout is
    a sibling of the package, and an installed tool falls back to the
    ``source_dir`` recorded in install.toml.
    """
    package_relative = Path(__file__).resolve().parent.parent / "gui"
    if package_relative.is_dir():
        return package_relative
    try:
        from vikram.update import load_metadata

        source_dir = load_metadata().get("source_dir")
    except Exception:
        source_dir = None
    if source_dir:
        candidate = Path(str(source_dir)) / "gui"
        if candidate.is_dir():
            return candidate
    return package_relative


def _bundle_executable(bundle: Path) -> Path | None:
    """The binary inside a .app.

    Launched directly rather than through ``open -a``: passing environment
    through ``open`` varies by macOS version, and the environment is the whole
    point of this launcher.
    """
    macos_dir = bundle / "Contents" / "MacOS"
    if not macos_dir.is_dir():
        return None
    executables = [p for p in sorted(macos_dir.iterdir()) if os.access(p, os.X_OK)]
    return executables[0] if executables else None


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vikram gui", description="Open the Vikram Studio desktop app."
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Run from the checkout with hot reload (needs npm).",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    api_binary = find_api_binary()
    if api_binary is None:
        print(
            "Could not find `vikram-api` on PATH.\n"
            "Install it with:  uv tool install --force --from . vikram",
            file=sys.stderr,
        )
        return 1

    env = {**os.environ, API_BIN_ENV: str(api_binary)}

    if args.dev:
        gui_dir = repo_gui_dir()
        if not (gui_dir / "package.json").is_file():
            print(f"No GUI sources at {gui_dir}.", file=sys.stderr)
            return 1
        if not (gui_dir / "node_modules").is_dir():
            print("Installing GUI dependencies…", file=sys.stderr)
            subprocess.run(["npm", "install"], cwd=gui_dir, env=env, check=True)
        return subprocess.run(
            ["npm", "run", "tauri", "dev"], cwd=gui_dir, env=env
        ).returncode

    bundle = find_bundle()
    if bundle is None:
        print(
            "Vikram Studio is not built yet.\n"
            "\n"
            "  Build it:      cd gui && npm install && npm run tauri build\n"
            "  Or run live:   vikram gui --dev\n"
            "\n"
            f"Looked in: {', '.join(BUNDLE_CANDIDATES)}, "
            f"and {bundle_build_output()}",
            file=sys.stderr,
        )
        return 1

    executable = _bundle_executable(bundle)
    if executable is None:
        print(f"No executable inside {bundle}.", file=sys.stderr)
        return 1

    # Detach: the app outlives this command, and leaving it attached to the
    # terminal both spams the shell with sidecar logs and keeps any process
    # capturing our output waiting on a pipe that never closes.
    log_path = studio_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        subprocess.Popen(
            [str(executable)],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    print(f"Opened {bundle}")
    print(f"Logs: {log_path}", file=sys.stderr)
    return 0


__all__ = [
    "bundle_build_output",
    "studio_log_path",
    "find_api_binary",
    "find_bundle",
    "repo_gui_dir",
    "run",
]
