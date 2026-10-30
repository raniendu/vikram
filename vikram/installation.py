"""Per-user installation of one versioned payload for the CLI and Mac app."""

import contextlib
import ctypes
import errno
import fcntl
import json
import os
import platform
import plistlib
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from importlib.metadata import version as package_version
from itertools import chain
from pathlib import Path
from uuid import uuid4

REPOSITORY = "raniendu/vikram"
RESERVE = 20 * 1024**3
RUNTIME_SPACE = 1024**3
MAX_EXPANDED = 512 * 1024**2
ROOT_NAME = "dev.raniendu.vikram.install"
MARKER = b"Vikram installer v1: raniendu/vikram\n"
# Which version the app in ~/Applications holds, relative to the install root.
APP_SOURCE = "app-source"
RENAME_SWAP = 0x2
PROFILE_BLOCK = b"""# >>> Vikram CLI >>>
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) export PATH="$HOME/.local/bin:$PATH" ;;
esac
# <<< Vikram CLI <<<
"""


class InstallError(Exception):
    """An actionable installation failure, safe to display to the user."""


def version_tuple(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(
        r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})", value
    ):
        raise InstallError("Expected a stable MAJOR.MINOR.PATCH release version.")
    return tuple(int(part) for part in value.split("."))


def cli_version() -> str:
    return package_version("vikram")


@dataclass(frozen=True)
class Layout:
    home: Path

    def __post_init__(self):
        object.__setattr__(self, "home", self.home.resolve())

    @property
    def root(self) -> Path:
        return self.home / "Library/Application Support" / ROOT_NAME

    @property
    def versions(self) -> Path:
        return self.root / "versions"

    @property
    def current(self) -> Path:
        return self.root / "current"

    @property
    def app(self) -> Path:
        return self.home / "Applications/Vikram.app"

    @property
    def cli(self) -> Path:
        return self.home / ".local/bin/vikram"

    @property
    def legacy_cli(self) -> Path:
        return self.cli.with_name("vikram.before-vikram-installer")


def runtime_layout() -> Layout:
    if getattr(sys, "frozen", False):
        binary = Path(sys.executable).resolve()
        if len(binary.parents) >= 9 and binary.parents[5].name == ROOT_NAME:
            layout = Layout(binary.parents[8])
            if layout.root == binary.parents[5]:
                return layout
        # The app in ~/Applications: .../Vikram.app/Contents/MacOS/vikram-sidecar
        if len(binary.parents) >= 5 and binary.parents[3].name == "Applications":
            layout = Layout(binary.parents[4])
            if layout.app == binary.parents[2] and (layout.root / APP_SOURCE).is_file():
                return layout
    return Layout(Path.home())


def installed_runtime(layout: Layout) -> bool:
    """Whether this process is the installed, frozen runtime of `layout`."""
    if not getattr(sys, "frozen", False):
        return False
    binary = Path(sys.executable).resolve()
    return binary.is_relative_to(layout.root.resolve()) or (
        layout.app.is_dir()
        and not layout.app.is_symlink()
        and binary.is_relative_to(layout.app)
    )


def check_platform() -> None:
    if (
        platform.system() != "Darwin"
        or platform.machine() != "arm64"
        or int(platform.mac_ver()[0].split(".")[0] or "0") < 13
    ):
        raise InstallError(
            "This installer requires Apple Silicon and macOS 13 or newer."
        )
    if os.geteuid() == 0:
        raise InstallError("Run the installer as your normal account, without sudo.")


def ensure_space(path: Path, additional: int = 0) -> None:
    while not path.exists():
        path = path.parent
    if shutil.disk_usage(path).free < RESERVE + RUNTIME_SPACE + additional:
        raise InstallError(
            "Not enough disk space. Free space must stay above 20 GiB, including "
            "the new bundle and temporary runtime files."
        )


def _directory(path: Path, home: Path) -> None:
    """Never follow a symlink while creating installer-owned destinations."""
    relatives = path.relative_to(home).parts
    for index in range(len(relatives) + 1):
        item = home.joinpath(*relatives[:index])
        if item.is_symlink():
            raise InstallError(f"Installation directory is a symlink: {item}")
        if not item.exists():
            item.mkdir(mode=0o700)
        info = item.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise InstallError(
                f"Installation directory must belong to your account: {item}"
            )


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    temporary = path.with_name(f".{path.name}-{uuid4().hex}")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def install_lock(layout: Layout):
    _directory(layout.root, layout.home)
    marker = layout.root / ".managed"
    if marker.is_symlink():
        raise InstallError("The installation marker is a symlink.")
    # Acquire the lock before initializing the marker, including a first install.
    fd = os.open(
        layout.root / "install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(fd, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise InstallError(
                "Another Vikram installation or update is already running."
            ) from None
        if marker.exists():
            if marker.stat().st_size != len(MARKER) or marker.read_bytes() != MARKER:
                raise InstallError("Unrecognized existing installation directory.")
        else:
            if set(p.name for p in layout.root.iterdir()) != {"install.lock"}:
                raise InstallError("Unrecognized existing installation directory.")
            _atomic_write(marker, MARKER)
        _directory(layout.versions, layout.home)
        yield


def bundle_version(app: Path) -> str:
    plist = app / "Contents/Info.plist"
    try:
        if plist.is_symlink() or plist.stat().st_size > 64 * 1024:
            raise ValueError()
        with plist.open("rb") as stream:
            info = plistlib.load(stream)
        result = info["CFBundleShortVersionString"]
        version_tuple(result)
        if (
            info["CFBundleIdentifier"] != "dev.raniendu.vikram"
            or info["CFBundleExecutable"] != "vikram-desktop"
        ):
            raise ValueError()
        for name in ("vikram-desktop", "vikram-sidecar"):
            binary = app / "Contents/MacOS" / name
            if (
                not binary.is_file()
                or binary.is_symlink()
                or not os.access(binary, os.X_OK)
            ):
                raise ValueError()
        return result
    except (OSError, ValueError, KeyError, TypeError, plistlib.InvalidFileException):
        raise InstallError("This is not a valid Vikram application bundle.") from None


def bundle_size(app: Path) -> int:
    total, count = 0, 0
    for path in chain([app], app.rglob("*")):
        info = path.lstat()
        count += 1
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise InstallError(
                "The application contains a symlink or unsupported file."
            )
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
        if total > MAX_EXPANDED or count > 10000:
            raise InstallError("The application exceeds the installation size limit.")
    return total


def verify_bundle(app: Path) -> None:
    try:
        subprocess.run(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
            capture_output=True,
            check=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        raise InstallError(
            "The application's code signature could not be verified."
        ) from None


def check_runtime(app: Path, version: str) -> None:
    binary = app / "Contents/MacOS/vikram-sidecar"
    try:
        with tempfile.TemporaryDirectory(prefix="vikram-install-check-") as library:
            env = {
                **os.environ,
                "VIKRAM_CHAT_DATA_DIR": library,
                "PYINSTALLER_RESET_ENVIRONMENT": "1",
            }
            cli = subprocess.run(
                [str(binary), "--cli", "--version"],
                text=True,
                capture_output=True,
                timeout=45,
                check=True,
                env=env,
            )
            if cli.stdout.strip() != f"Vikram {version}":
                raise ValueError()
            desktop = subprocess.run(
                [str(binary)],
                text=True,
                capture_output=True,
                timeout=45,
                input='{"v":1,"id":"install-check","method":"hello"}\n',
                check=True,
                env=env,
            )
            if json.loads(desktop.stdout)["result"]["protocol"] != 1:
                raise ValueError()
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        raise InstallError(
            "The new CLI or desktop service failed its startup check. The current version is unchanged."
        ) from None


def installed_version(layout: Layout) -> str | None:
    if not os.path.lexists(layout.current):
        return None
    if not layout.current.is_symlink():
        raise InstallError("Unrecognized existing installation pointer.")
    target = os.readlink(layout.current)
    if not re.fullmatch(r"versions/[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{32}", target):
        raise InstallError("Unrecognized existing installation pointer.")
    destination = layout.root / target
    if destination.is_symlink() or layout.versions.is_symlink():
        raise InstallError("The installed version directory is a symlink.")
    return bundle_version(destination / "Vikram.app")


def login_profile(home: Path) -> str | None:
    if (
        os.environ.get("ZDOTDIR")
        and Path(os.environ["ZDOTDIR"]).expanduser().resolve() != home
    ):
        return None
    shell = Path(pwd.getpwuid(os.getuid()).pw_shell).name
    if shell == "bash":
        return next(
            (
                name
                for name in (".bash_profile", ".bash_login", ".profile")
                if os.path.lexists(home / name)
            ),
            ".bash_profile",
        )
    return ".zprofile" if shell == "zsh" else None


def launcher(layout: Layout) -> bytes:
    runtime = layout.current / "Vikram.app/Contents/MacOS/vikram-sidecar"
    return f"""#!/bin/sh
# Vikram managed CLI v1
runtime={shlex.quote(str(runtime))}
if [ -x "$runtime" ]; then
  exec "$runtime" --cli "$@"
fi
legacy={shlex.quote(str(layout.legacy_cli))}
if [ -x "$legacy" ]; then
  exec "$legacy" "$@"
fi
printf '%s\\n' 'Vikram installation is incomplete. Open Install Vikram.app again.' >&2
exit 1
""".encode()


def _preflight_links(layout: Layout) -> tuple[bool, Path | None]:
    _directory(layout.app.parent, layout.home)
    _directory(layout.cli.parent, layout.home)
    if os.path.lexists(layout.app) and not managed_app(layout):
        raise InstallError(
            f"Move the existing {layout.app} aside, then run the installer again."
        )
    migrate = False
    if os.path.lexists(layout.cli):
        if layout.cli.is_symlink():
            expected = layout.home / ".local/share/uv/tools/vikram/bin/vikram"
            migrate = layout.cli.resolve() == expected.resolve()
            valid_backup = layout.legacy_cli.is_symlink() and os.readlink(
                layout.legacy_cli
            ) == os.readlink(layout.cli)
            if not migrate or (os.path.lexists(layout.legacy_cli) and not valid_backup):
                raise InstallError(
                    f"Preserve the existing {layout.cli} elsewhere before installing."
                )
        elif (
            not layout.cli.is_file()
            or layout.cli.stat().st_size > 8192
            or layout.cli.read_bytes() != launcher(layout)
        ):
            raise InstallError(
                f"Preserve the existing {layout.cli} elsewhere before installing."
            )
    name = login_profile(layout.home)
    profile = layout.home / name if name else None
    if profile and (
        profile.is_symlink()
        or (
            profile.exists()
            and (not profile.is_file() or profile.stat().st_size > 1024 * 1024)
        )
    ):
        raise InstallError(
            f"The shell profile {profile} is linked or unsupported. Add ~/.local/bin to PATH manually, or use a regular profile before installing."
        )
    return migrate, profile


def activate(layout: Layout, release: Path) -> None:
    temporary = layout.root / f".current-{uuid4().hex}"
    try:
        temporary.symlink_to(release.relative_to(layout.root))
        os.replace(temporary, layout.current)
    finally:
        temporary.unlink(missing_ok=True)


def managed_app(layout: Layout) -> bool:
    """Whether ~/Applications/Vikram.app is the installer's: the app folder it
    keeps current, or the link to `current` that earlier versions made."""
    app = layout.app
    if app.is_symlink():
        return os.readlink(app) == str(layout.current / "Vikram.app")
    if not app.is_dir() or not (layout.root / APP_SOURCE).is_file():
        return False
    try:
        bundle_version(app)
    except InstallError:
        return False
    return True


def _clone(source: Path, destination: Path) -> None:
    """Copy an app; on APFS, a copy-on-write clone that takes no extra space."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.clonefile(os.fsencode(source), os.fsencode(destination), 0x1) == 0:
        return
    shutil.rmtree(destination, ignore_errors=True)
    shutil.copytree(source, destination, copy_function=shutil.copy2, symlinks=True)


def _swap(first: Path, second: Path) -> None:
    """Exchange two folders in one step, so the app is never half-replaced."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.renamex_np(os.fsencode(first), os.fsencode(second), RENAME_SWAP) == 0:
        return
    code = ctypes.get_errno()
    if code not in (errno.ENOTSUP, errno.EINVAL, errno.EXDEV):
        raise OSError(code, os.strerror(code))
    # A file system without swaps: two renames, a moment apart.
    aside = first.with_name(f".{first.name}-{uuid4().hex}")
    os.rename(second, aside)
    os.rename(first, second)
    os.rename(aside, first)


def publish_app(layout: Layout) -> bool:
    """Make ~/Applications/Vikram.app hold the active version; True if it changed.

    The folder stays in place and only its Contents are exchanged, so the Dock,
    Spotlight, and Finder keep opening it after every update. (macOS records
    where a link points when the app is added to the Dock, so a link to
    `current` would keep opening the version that was current then.) A running
    app keeps the files it has open; it uses the new version once reopened.
    """
    target = os.readlink(layout.current)
    stamp = layout.root / APP_SOURCE
    app = layout.app
    if (
        app.is_dir()
        and not app.is_symlink()
        and stamp.is_file()
        and stamp.read_text() == target
    ):
        return False
    if os.path.lexists(app) and not managed_app(layout):
        raise InstallError(f"Move the existing {app} aside, then update again.")
    _directory(app.parent, layout.home)
    source = layout.root / target / "Vikram.app"
    staging = Path(tempfile.mkdtemp(prefix=".app-", dir=layout.root))
    try:
        staged = staging / "Vikram.app"
        _clone(source, staged)
        if bundle_version(staged) != bundle_version(source):
            raise InstallError("The app copy does not match the installed version.")
        if app.is_dir() and not app.is_symlink():
            _swap(staged / "Contents", app / "Contents")
            os.utime(app)
            _atomic_write(stamp, target.encode())
        else:
            # Recorded first: an interruption must not leave a folder that a
            # later install would not recognize as its own.
            _atomic_write(stamp, target.encode())
            if app.is_symlink():
                app.unlink()
            os.rename(staged, app)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if layout.home != Path.home().resolve():
        return True  # A disposable test account: leave LaunchServices alone.
    # Let Finder and Spotlight show the new version; harmless if it fails.
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            [
                "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
                "LaunchServices.framework/Support/lsregister",
                "-f",
                str(app),
            ],
            capture_output=True,
            timeout=30,
        )
    return True


def _refresh_scheduler(layout: Layout) -> None:
    """Keep an existing scheduled-task agent's definition current.

    It already runs the new version through `current`; never fail an install
    that has already switched over because of it.
    """
    from vikram.schedules import refresh_installed_agent

    with contextlib.suppress(OSError, subprocess.SubprocessError):
        refresh_installed_agent(layout.home)


def install_bundle(source: Path, layout: Layout, *, _locked: bool = False) -> str:
    check_platform()
    ensure_space(layout.home, MAX_EXPANDED)
    # The updater holds this same lock while downloading and extracting.
    with contextlib.nullcontext() if _locked else install_lock(layout):
        current = installed_version(layout)
        version = bundle_version(source)
        if current and version_tuple(version) < version_tuple(current):
            raise InstallError(f"Refusing to downgrade Vikram {current} to {version}.")
        size = bundle_size(source)
        verify_bundle(source)
        migrate, profile = _preflight_links(layout)
        ensure_space(layout.root, size)
        release = layout.versions / f"{version}-{uuid4().hex}"
        changed, backup_created = [], False

        def activated() -> bool:
            try:
                return os.readlink(layout.current) == str(
                    release.relative_to(layout.root)
                )
            except FileNotFoundError:
                return False

        try:
            with tempfile.TemporaryDirectory(
                prefix=".stage-", dir=layout.versions
            ) as temporary:
                staged = Path(temporary) / "Vikram.app"
                shutil.copytree(source, staged, copy_function=shutil.copy2)
                # Validate the copied bytes before any of the launch paths change.
                bundle_size(staged)
                if bundle_version(staged) != version:
                    raise InstallError("The bundle version changed while staging.")
                verify_bundle(staged)
                check_runtime(staged, version)
                ensure_space(layout.root)
                release.mkdir(mode=0o700)
                os.replace(staged, release / "Vikram.app")
            if migrate and not os.path.lexists(layout.legacy_cli):
                # A backup also provides a working fallback if first install is interrupted.
                layout.legacy_cli.symlink_to(os.readlink(layout.cli))
                backup_created = True
            if not layout.cli.exists() or migrate:
                old = os.readlink(layout.cli) if migrate else None
                _atomic_write(layout.cli, launcher(layout), 0o755)
                changed.append((layout.cli, old, None))
            if profile:
                old = profile.read_bytes() if profile.exists() else None
                if b"# >>> Vikram CLI >>>" not in (old or b""):
                    mode = (
                        stat.S_IMODE(profile.stat().st_mode)
                        if profile.exists()
                        else 0o600
                    )
                    _atomic_write(profile, (old or b"") + b"\n" + PROFILE_BLOCK, mode)
                    changed.append((profile, old, mode))
            activate(layout, release)
            # After the switch: a failure here leaves the app at the previous
            # version, and the next install or `vikram update` repairs it.
            with contextlib.suppress(OSError, InstallError):
                publish_app(layout)
            _refresh_scheduler(layout)
            return version
        except BaseException:
            # The rename is the commit point. A signal immediately after it
            # must not turn cleanup into deletion of the now-active release.
            if activated():
                return version
            raise
        finally:
            if not activated():
                for path, old, mode in reversed(changed):
                    if isinstance(old, bytes):
                        _atomic_write(path, old, mode)
                    else:
                        path.unlink(missing_ok=True)
                        if isinstance(old, str):
                            path.symlink_to(old)
                if backup_created and layout.cli.is_symlink():
                    layout.legacy_cli.unlink(missing_ok=True)
                if release.exists():
                    shutil.rmtree(release)
