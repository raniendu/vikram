"""Run model-written Python in a local microVM, isolated from this Mac.

Each reply that runs code gets its own VM, booted on first use from a bundled,
digest-pinned Python image and deleted when the reply ends. It uses Pydantic AI
Harness `SmolSandbox` (smolvm on Apple's Hypervisor framework): its own Linux
kernel, no network, nothing from the Mac mounted, and no host environment.
"""

import asyncio
import dataclasses
import functools
import hashlib
import json
import os
import platform
import shutil
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

IMAGE_REPOSITORY = "library/python"
IMAGE_TAG = "3.13-alpine"
# The linux/arm64 manifest, so the archive's content is fixed and verifiable.
IMAGE_MANIFEST = (
    "sha256:e041b488453556787172f43845c9e0e90b9fb4a91452498420fb2011b23241b6"
)
IMAGE_FILE = "python-3.13-alpine-arm64.tar"
REGISTRY_MANIFEST = "image-manifest.json"
VM_CPUS, VM_MEMORY_MB = 1, 512
# Each of the VM's two disks, sparse files on this Mac. smolvm's default (19.6 GB)
# would let one snippet fill the Mac's disk: the VM writes over 1 GB/s.
VM_DISK_GB = 1
# The free space Vikram always leaves on the Mac's disk.
RESERVE = 20 * 1024**3
RUN_TIMEOUT = 15
# Runs a snippet like the Python REPL: a final bare expression is printed.
RUNNER = """
import ast, sys, traceback
path = sys.argv[1]
try:
    tree = ast.parse(open(path).read(), "<code>")
except SyntaxError as error:
    sys.stderr.write("".join(traceback.format_exception_only(error)))
    sys.exit(1)
tail = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
scope = {"__name__": "__main__"}
try:
    exec(compile(tree, "<code>", "exec"), scope)
    if tail is not None:
        value = eval(compile(ast.Expression(tail.value), "<code>", "eval"), scope)
        if value is not None:
            print(repr(value))
except SystemExit:
    raise
except BaseException as error:
    trace = error.__traceback__.tb_next if error.__traceback__ else None
    sys.stderr.write("".join(traceback.format_exception(type(error), error, trace)))
    sys.exit(1)
"""


class SandboxUnavailable(Exception):
    """The VM could not start; the message is safe to show and to give the model."""


@dataclass(frozen=True)
class CodeRun:
    exit_code: int | None
    output: str
    error: str
    timed_out: bool = False


class Sandbox(Protocol):
    async def run(self, code: str, /) -> CodeRun: ...

    async def close(self) -> None: ...


def image_path() -> Path | None:
    """The bundled image in an installed app, or the one fetched in a checkout."""
    candidates = [Path(__file__).resolve().parents[1] / "build/sandbox" / IMAGE_FILE]
    if getattr(sys, "frozen", False):
        candidates.insert(0, Path(sys._MEIPASS) / "sandbox" / IMAGE_FILE)
    return next((path for path in candidates if path.is_file()), None)


def verify_archive(path: Path) -> bool:
    """Every file matches the pinned registry manifest, digest by digest."""
    try:
        with tarfile.open(path) as archive:
            files = {
                member.name: archive.extractfile(member).read()
                for member in archive.getmembers()
                if member.isfile()
            }
        registry = files[REGISTRY_MANIFEST]
        if f"sha256:{hashlib.sha256(registry).hexdigest()}" != IMAGE_MANIFEST:
            return False
        manifest = json.loads(registry)
        [entry] = json.loads(files["manifest.json"])
        digests = [
            manifest["config"]["digest"],
            *(l["digest"] for l in manifest["layers"]),
        ]
        if [entry["Config"], *entry["Layers"]] != [
            digest.replace(":", "-") for digest in digests
        ]:
            return False
        return all(
            f"sha256:{hashlib.sha256(files[d.replace(':', '-')]).hexdigest()}" == d
            for d in digests
        )
    except (OSError, KeyError, ValueError, TypeError, tarfile.TarError):
        return False


def _pin_guest_rootfs() -> None:
    """Give a frozen app's smolvm guest filesystem a modification time from its content.

    smolvm finds its helper and libraries beside its package, even in a frozen
    app, so nothing is exported to child processes. It unpacks its guest
    filesystem (46 MB) into a cache keyed by the file's size and modification
    time, and a frozen app unpacks its files with new times at every launch, so
    each launch that ran code would leave another copy behind.
    """
    if not getattr(sys, "frozen", False):
        return
    for name in ("agent-rootfs.tar.zst", "agent-rootfs.tar"):
        path = Path(sys._MEIPASS) / "smol" / name
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).digest()
            stamp = 1_000_000_000 + int.from_bytes(digest[:4]) % 10**9
            os.utime(path, (stamp, stamp))
            return


@functools.cache
def unavailable_reason() -> str | None:
    """Why this host cannot run the sandbox, or `None` when it can."""
    if not (sys.platform == "darwin" and platform.machine() == "arm64"):
        return "The Python sandbox needs an Apple silicon Mac."
    # Before smolvm first loads, in case it reads the time when configured.
    _pin_guest_rootfs()
    path = image_path()
    if path is None:
        return (
            "The Python sandbox image is missing. Rebuild Vikram, or run "
            "`uv run python scripts/fetch_sandbox_image.py` in a checkout."
        )
    if not verify_archive(path):
        return "The Python sandbox image is damaged. Rebuild or reinstall Vikram."
    try:
        import smol

        available, code, _ = smol.local_availability()
    except ImportError:
        return "The Python sandbox runtime is not installed."
    except Exception as error:
        available, code = False, type(error).__name__
    if not available:
        return f"This Mac cannot start sandbox VMs ({code})."
    return None


def _markers() -> Path:
    # Vikram's own record of the VMs it created, so a crash leaves nothing behind.
    return Path.home() / "Library/Caches/dev.raniendu.vikram/sandbox-vms"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


_swept = False


async def _sweep() -> None:
    """Delete VMs that a crashed Vikram process left behind, once per process."""
    global _swept
    if _swept:
        return
    _swept = True
    from pydantic_ai.workspaces import WorkspaceRef
    from pydantic_ai_harness.smol_sandbox import SmolSandbox

    for marker in _markers().glob("*.json"):
        try:
            pid = json.loads(marker.read_text())["pid"]
            ref = WorkspaceRef(provider="smol", id=marker.stem)
        except (OSError, ValueError, KeyError, TypeError):
            marker.unlink(missing_ok=True)  # Unreadable: there is nothing to delete.
            continue
        if _alive(pid):
            continue
        try:
            # A VM that is already gone returns quietly.
            await SmolSandbox().destroy(ref)
        except ValueError:
            pass  # Not a VM name Vikram creates.
        except Exception:
            continue  # Keep the marker and try again next time.
        marker.unlink(missing_ok=True)


def _describe(error: BaseException) -> str:
    # smolvm's error code when there is one; never a message, which can hold paths.
    for cause in (error, error.__cause__):
        if isinstance(code := getattr(cause, "code", None), str) and code:
            return code
    return type(error).__name__


class VmSandbox:
    """One microVM for one reply: booted on first use, deleted by `close()`."""

    def __init__(self) -> None:
        self._backend = None
        self._workspace = None
        self._broken: str | None = None
        self._boot: asyncio.Task | None = None
        # One snippet at a time: each run rewrites the same file in the VM.
        self._lock = asyncio.Lock()

    async def _start(self):
        from pydantic_ai.workspaces import Workspace
        from pydantic_ai_harness.smol_sandbox import SmolSandboxBackend

        if shutil.disk_usage(Path.home()).free - 2 * VM_DISK_GB * 1024**3 < RESERVE:
            raise SandboxUnavailable(
                "The Python sandbox needs more free disk space to keep the 20 GiB "
                "reserve."
            )
        await _sweep()
        backend = SmolSandboxBackend(
            image=str(image_path()), cpus=VM_CPUS, memory_mb=VM_MEMORY_MB
        )
        # The Harness leaves disk sizes to smolvm; Harness 0.56 keeps the machine's
        # resources here. Refuse to boot rather than run with the default disks.
        resources = getattr(backend, "_resources", None)
        if not dataclasses.is_dataclass(resources) or not hasattr(
            resources, "storage_gb"
        ):
            raise SandboxUnavailable("The Python sandbox cannot limit its disk size.")
        backend._resources = dataclasses.replace(
            resources, storage_gb=VM_DISK_GB, overlay_gb=VM_DISK_GB
        )
        self._backend = backend
        workspace = Workspace(backend)
        boot = self._boot = asyncio.ensure_future(workspace.working_dir())
        marked = False
        try:
            await asyncio.sleep(0)  # The ref exists once the boot has started.
            self._mark()
            marked = True
            await boot
        except BaseException as error:
            boot.cancel()
            await asyncio.gather(boot, return_exceptions=True)
            self._boot = None
            if not marked or isinstance(error, asyncio.CancelledError):
                await self.close()
            if isinstance(error, asyncio.CancelledError):
                raise
            self._broken = f"The Python sandbox could not start ({_describe(error)})."
            raise SandboxUnavailable(self._broken) from None
        finally:
            self._boot = None
        self._workspace = workspace
        return workspace

    def _mark(self) -> None:
        ref = self._backend.ref if self._backend else None
        if ref is not None:
            _markers().mkdir(parents=True, exist_ok=True)
            (_markers() / f"{ref.id}.json").write_text(json.dumps({"pid": os.getpid()}))

    async def run(self, code: str, /) -> CodeRun:
        async with self._lock:
            return await self._run(code)

    async def _run(self, code: str) -> CodeRun:
        from pydantic_ai.workspaces import (
            WorkspaceOutputLimitError,
            WorkspaceTimeoutError,
            WorkspaceUnavailableError,
        )

        if self._broken:
            raise SandboxUnavailable(self._broken)
        workspace = self._workspace or await self._start()
        try:
            await workspace.write_text("snippet.py", code)
            result = await workspace.run(
                ["python", "-c", RUNNER, "snippet.py"], timeout=RUN_TIMEOUT
            )
        except WorkspaceTimeoutError:
            return CodeRun(None, "", f"Stopped after {RUN_TIMEOUT} seconds.", True)
        except WorkspaceOutputLimitError:
            return CodeRun(None, "", "Stopped: the output exceeded 10 MiB.")
        except WorkspaceUnavailableError:
            self._broken = "The Python sandbox stopped unexpectedly."
            raise SandboxUnavailable(self._broken) from None
        return CodeRun(result.exit_code, result.stdout, result.stderr)

    async def close(self) -> None:
        boot, self._boot = self._boot, None
        if boot is not None and not boot.done():
            boot.cancel()
            await asyncio.gather(boot, return_exceptions=True)
        backend, self._backend, self._workspace = self._backend, None, None
        if backend is None or backend.ref is None:
            return
        ref = backend.ref
        from pydantic_ai_harness.smol_sandbox import SmolSandbox

        try:
            await SmolSandbox().destroy(ref)
        except Exception:
            return  # Keep the marker: the next process sweeps it.
        try:
            (_markers() / f"{ref.id}.json").unlink(missing_ok=True)
        except OSError:
            pass  # A startup failure may mean the marker directory is inaccessible.


COMPOSITION_FRAME_LIMIT = 1024 * 1024
# This control program runs in the guest through the existing VM control API.
# It never evaluates request contents and never reads more than one bounded batch.
_POLL_COMPOSITION = r"""
import json, os, re, stat, sys
root, maximum = sys.argv[1], int(sys.argv[2])
frames, total = [], 2
with os.scandir(root) as scan:
    for entry in scan:
        if len(frames) >= 5:
            break
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}\.request", entry.name):
            continue
        fd = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("Invalid broker frame")
            data = source.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("Broker frame exceeds limit")
        if frames and total + len(data) > maximum - 100:
            break
        frame = json.loads(data)
        if not isinstance(frame, dict) or frame.get("id") != entry.name[:-8]:
            raise ValueError("Invalid broker frame identity")
        frames.append(frame)
        total += len(data) + 2
        os.replace(entry.path, entry.path + ".claimed")
print(json.dumps(frames, separators=(",", ":")))
"""


class CompositionSandbox(VmSandbox):
    """Separate per-composition VM with bounded JSON control files, never mounts."""

    async def prepare(self, code: str, tools: list[dict], execution_id: str) -> None:
        from vikram.composition import GUEST_RUNNER

        if not execution_id.isalnum() or len(execution_id) > 64:
            raise ValueError("Invalid composition identifier.")
        reason = unavailable_reason()
        if reason:
            raise SandboxUnavailable(reason)
        workspace = self._workspace or await self._start()
        self._control = f"composition/{execution_id}"
        await workspace.write_text(f"{self._control}/code.py", code)
        await workspace.write_text(f"{self._control}/tools.json", json.dumps(tools))
        await workspace.write_text(f"{self._control}/runner.py", GUEST_RUNNER)

    async def run_composition(self) -> CodeRun:
        from pydantic_ai.workspaces import (
            WorkspaceOutputLimitError,
            WorkspaceTimeoutError,
        )

        try:
            result = await self._workspace.run(
                ["python", f"{self._control}/runner.py", self._control], timeout=30 * 60
            )
        except WorkspaceTimeoutError:
            return CodeRun(None, "", "Composition exceeded its overall deadline.", True)
        except WorkspaceOutputLimitError:
            return CodeRun(None, "", "Composition exceeded the VM output limit.")
        return CodeRun(result.exit_code, result.stdout, result.stderr)

    async def poll_requests(self) -> list[dict]:
        result = await self._workspace.run(
            [
                "python",
                "-c",
                _POLL_COMPOSITION,
                self._control,
                str(COMPOSITION_FRAME_LIMIT),
            ],
            timeout=5,
        )
        if result.exit_code or len(result.stdout.encode()) > COMPOSITION_FRAME_LIMIT:
            raise ValueError(
                "The composition broker received an invalid or oversized frame."
            )
        frames = json.loads(result.stdout)
        if not isinstance(frames, list) or len(frames) > 5:
            raise ValueError("The composition broker received an invalid batch.")
        return frames

    async def write_response(self, identifier: str, result: dict) -> None:
        if (
            not identifier
            or len(identifier) > 100
            or any(not (c.isascii() and (c.isalnum() or c in "_-")) for c in identifier)
        ):
            raise ValueError("Invalid composition call identifier.")
        encoded = json.dumps(result, allow_nan=False)
        if len(encoded.encode()) > COMPOSITION_FRAME_LIMIT:
            raise ValueError("The composition result exceeds the frame limit.")
        # Workspace writes publish atomically through its guest filesystem API.
        await self._workspace.write_text(
            f"{self._control}/{identifier}.response", encoded
        )


def host_sandbox() -> Sandbox | None:
    """A new VM sandbox for one reply, or `None` when this host cannot run one."""
    return VmSandbox() if unavailable_reason() is None else None


def check_sandbox() -> int:
    """Boot a VM and run one snippet without a model, to check a packaged runtime."""
    reason = unavailable_reason()
    if reason is None:

        async def probe() -> CodeRun:
            sandbox = VmSandbox()
            try:
                return await sandbox.run("sum(range(10))")
            finally:
                await sandbox.close()

        try:
            run = asyncio.run(probe())
            if run.output.strip() != "45":
                reason = "The Python sandbox returned an unexpected result."
        except SandboxUnavailable as error:
            reason = str(error)
    if reason is not None:
        sys.stderr.write(f"{reason}\n")
        return 1
    sys.stdout.write("The Python sandbox is ready.\n")
    return 0
